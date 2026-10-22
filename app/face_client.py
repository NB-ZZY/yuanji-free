"""腾讯云人脸识别（IAI v2020-03-03）：建库 / 入库 / 搜脸 / 测试连接。

设计：
- 每个班级一个人员库 GroupId = f"kidobs_{classId}"，PersonId = f"s{studentId}"。
- 单人最多 5 张脸（腾讯限制）。超过时按"先入先出"删除最早的脸再补录，
  这样孩子长大后老师随手补几张新照片，库里始终是最近的样子。
- 可选服务：由用户自行开通和配置；未启用时使用人工选择幼儿。
"""
import base64
import json
import logging
import time
from typing import Optional

from config import (FACE_MATCH_THRESHOLD, FACE_MAX_PER_PERSON, FACE_MODEL_VERSION, MOCK,
                    tencent_creds)

log = logging.getLogger("face")


class FaceError(Exception):
    def __init__(self, msg: str, code: str = ""):
        super().__init__(msg)
        self.code = code


def enabled():
    from config import face_enabled
    return face_enabled()


def _client():
    if not enabled():
        raise FaceError('未启用腾讯云人脸识别，请在设置中配置，或手动选择幼儿。', 'DISABLED')
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.iai.v20200303 import iai_client
    sid, skey, region = tencent_creds()
    if not sid or not skey:
        raise FaceError("请在设置中填写自己的腾讯云 SecretId 和 SecretKey", "NOKEY")
    cred = credential.Credential(sid, skey)
    hp = HttpProfile(); hp.endpoint = "iai.tencentcloudapi.com"; hp.reqTimeout = 30
    cp = ClientProfile(); cp.httpProfile = hp
    return iai_client.IaiClient(cred, region, cp)


def _call(name: str, **params):
    """统一调用：返回 dict；把 SDK 异常转成 FaceError（带中文说明）。"""
    if not enabled():
        raise FaceError('人脸识别未启用，可手动选择幼儿。', 'DISABLED')
    from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
    from tencentcloud.iai.v20200303 import models
    req = getattr(models, name + "Request")()
    req.from_json_string(json.dumps(params))
    try:
        resp = getattr(_client(), name)(req)
        return json.loads(resp.to_json_string())
    except TencentCloudSDKException as e:
        code = e.code or ""
        friendly = {
            "AuthFailure.SignatureFailure": "密钥不正确",
            "AuthFailure.SecretIdNotFound": "SecretId 不存在",
            "UnauthorizedOperation": "该账号没有所需的人脸识别权限，请按腾讯云配置教程检查子用户策略",
            "FailedOperation.ServiceNotActivated": "腾讯云人脸识别服务未开通",
            "InvalidParameterValue.NoFaceInPhoto": "照片里没有检测到人脸",
            "InvalidParameterValue.NoFaceInPhotoError": "照片里没有检测到人脸",
            "InvalidParameterValue.FaceSizeTooSmall": "人脸太小，请换一张更清晰/更近的照片",
            "InvalidParameterValue.ImageSizeExceed": "图片太大（限 5MB）",
            "FailedOperation.ImageDecodeFailed": "图片无法解码",
            "InvalidParameterValue.GroupIdAlreadyExist": "人员库已存在",
            "InvalidParameterValue.PersonIdAlreadyExist": "人员已存在",
            "InvalidParameterValue.GroupIdNotExist": "人员库不存在",
            "InvalidParameterValue.PersonIdNotExist": "人员不存在",
            "FailedOperation.FaceNumExceed": "该人员脸数已达上限",
            "InvalidParameterValue.FaceIdNotExist": "人脸不存在",
            "ResourceUnavailable.InArrears": "腾讯云账号欠费",
            "ResourceUnavailable.Freeze": "腾讯云账号被冻结",
            "LimitExceeded.ErrorFaceNumExceed": "该人员脸数已达上限",
        }.get(code, "")
        raise FaceError(f"{friendly or '人脸服务错误'}（{code}: {e.message}）", code) from e
    except Exception as e:  # 网络等
        raise FaceError(f"无法连接腾讯云人脸服务：{e}", "NETWORK") from e


def test_connection() -> dict:
    """设置页那个「测试人脸服务」按钮：只做一次最轻的调用，确认密钥和权限都对。"""
    from config import MOCK
    if MOCK:
        return {"ok": True, "groups": 0, "mock": True}
    r = _call("GetGroupList", Offset=0, Limit=10)
    return {"ok": True, "groups": int(r.get("GroupNum") or len(r.get("GroupInfos") or []))}


# ---------- 库管理 ----------
def group_id(class_id: int) -> str:
    return f"kidobs_{class_id}"


def person_id(student_id: int) -> str:
    return f"s{student_id}"


def ensure_group(class_id: int, class_name: str):
    if MOCK:
        return
    try:
        # 库名带上 classId，避免和别的班重名（腾讯要求库名全局唯一）
        name = f"{class_name[:40]}_{class_id}" if class_name else group_id(class_id)
        _call("CreateGroup", GroupName=name, GroupId=group_id(class_id),
              FaceModelVersion=FACE_MODEL_VERSION)
    except FaceError as e:
        if "AlreadyExist" not in (e.code or ""):
            raise


def group_face_counts(class_id: int) -> dict:
    """一次问清楚这个班在云端已经有哪些孩子、各有几张脸。

    为什么要有这个：人脸库本来就存在腾讯云上、按班级分组（kidobs_班级号），
    所以同一个班的第二位老师其实**不用重新导照片**——云端早就有了。
    但"已入库"这件事以前只记在第一位老师自己电脑的库里，
    第二位老师打开看到的全是灰点，会以为要从头再导一遍。
    这个函数就是去云端把真实情况读回来，一个班只花一次调用。

    返回 {student_id: 脸数}。班里没人入过库就返回空字典。
    """
    if MOCK:
        return {}
    out, offset = {}, 0
    while True:
        try:
            r = _call("GetPersonList", GroupId=group_id(class_id), Offset=offset, Limit=100)
        except FaceError as e:
            if "NotExist" in (e.code or ""):     # 这个班还没建过库 = 谁都没入过
                return {}
            raise
        infos = r.get("PersonInfos") or []
        for it in infos:
            pid = str(it.get("PersonId") or "")
            if not pid.startswith("s"):
                continue
            try:
                sid = int(pid[1:])
            except ValueError:
                continue
            out[sid] = len(it.get("FaceIds") or [])
        if len(infos) < 100:
            break
        offset += len(infos)
        if offset > 2000:                        # 保险丝，别被异常分页拖死
            break
    return out


def person_exists(student_id: int) -> bool:
    if MOCK:
        return False
    try:
        _call("GetPersonBaseInfo", PersonId=person_id(student_id))
        return True
    except FaceError as e:
        if "NotExist" in (e.code or ""):
            return False
        raise


def list_faces(class_id: int, student_id: int) -> list[str]:
    """返回该人员在云端的 FaceId 列表（按入库顺序）。"""
    if MOCK:
        return []
    r = _call("GetPersonBaseInfo", PersonId=person_id(student_id))
    return list(r.get("FaceIds") or [])


def delete_face(student_id: int, face_id: str):
    if MOCK:
        return
    _call("DeleteFace", PersonId=person_id(student_id), FaceIds=[face_id])


def delete_person(student_id: int):
    """删掉整个人员（腾讯不允许把一个人员的最后一张脸删光，只能连人员一起删）。"""
    if MOCK:
        return
    try:
        _call("DeletePerson", PersonId=person_id(student_id))
    except FaceError as e:
        if "NotExist" not in (e.code or ""):
            raise


def add_face(class_id: int, student_id: int, name: str, image_bytes: bytes) -> str:
    """入库一张脸，返回 FaceId。超过 5 张时自动删掉最早的一张。"""
    if MOCK:
        return f"mock_face_{int(time.time()*1000)}"
    img_b64 = base64.b64encode(image_bytes).decode()
    if not person_exists(student_id):
        # 第一张脸：随人员一起创建（腾讯要求 CreatePerson 必须带一张脸）
        try:
            r = _call("CreatePerson", GroupId=group_id(class_id), PersonName=name[:60], PersonId=person_id(student_id),
                      Gender=0, Image=img_b64, NeedRotateDetection=1, QualityControl=0, UniquePersonControl=0)
            return r["FaceId"]
        except FaceError as e:
            if "AlreadyExist" not in (e.code or ""):
                raise      # 并发/重试导致人员已建好，继续走加脸流程
    faces = list_faces(class_id, student_id)
    while len(faces) >= FACE_MAX_PER_PERSON:
        delete_face(student_id, faces.pop(0))
    # FaceMatchThreshold=0：不校验新脸与旧脸是否同一人（老师亲自指定的，信任老师）
    r = _call("CreateFace", PersonId=person_id(student_id), Images=[img_b64],
              NeedRotateDetection=1, QualityControl=0, FaceMatchThreshold=0)
    if r.get("SucFaceNum", 0) < 1:
        msg = (r.get("RetCode") or [None])[0]
        raise FaceError({
            -1101: "照片里没有检测到人脸",
            -1102: "图片解码失败",
            -1601: "图片质量不达标（模糊/过暗/遮挡），请换一张",
            -1602: "照片里有多张脸，请裁剪成只含这个孩子的照片",
            -1603: "照片里没有检测到人脸",
            -1604: "人脸太小，请换一张更清晰/更近的照片",
            -1605: "图片解码失败",
        }.get(msg, f"入库失败（{msg}）"))
    # 腾讯 CreateFace 返回的是 SucFaceIds（不是 FaceIds），别写错
    ids = r.get("SucFaceIds") or r.get("FaceIds") or []
    if not ids:
        raise FaceError("入库失败：腾讯云没有返回人脸 ID")
    return ids[0]


def search(class_id: int, image_bytes: bytes, max_faces: int = 6) -> list[dict]:
    """在班级库里搜脸。返回每张检测到的脸的最佳匹配：
       [{student_id, score, matched, face_rect}]，按脸面积从大到小。"""
    if MOCK:
        return [{"student_id": None, "score": 0, "matched": False, "face_rect": None, "mock": True}]
    # MinFaceSize 放小一点：合影里站得远的孩子脸只有几十像素，默认 34 会直接漏掉，
    # 漏掉就等于老师少了一条空位可选，宁可多框出来让老师删。
    r = _call("SearchFaces", GroupIds=[group_id(class_id)], Image=base64.b64encode(image_bytes).decode(),
              MaxFaceNum=max_faces, MaxPersonNum=1, NeedPersonInfo=0, NeedRotateDetection=1,
              QualityControl=0, MinFaceSize=20)
    out = []
    for res in r.get("Results") or []:
        rect = res.get("FaceRect") or {}
        cands = res.get("Candidates") or []
        best = cands[0] if cands else None
        score = float(best["Score"]) if best else 0.0
        sid = int(best["PersonId"][1:]) if best and best["PersonId"].startswith("s") else None
        out.append({
            "student_id": sid if score >= FACE_MATCH_THRESHOLD else None,
            "candidate_id": sid, "score": round(score, 1),
            "matched": score >= FACE_MATCH_THRESHOLD,
            "face_rect": rect,
            "area": rect.get("Width", 0) * rect.get("Height", 0),
        })
    out.sort(key=lambda x: -x["area"])
    return out


def detect_count(image_bytes: bytes) -> int:
    if MOCK:
        return 1
    r = _call("DetectFace", Image=base64.b64encode(image_bytes).decode(), MaxFaceNum=10, NeedFaceAttributes=0, NeedQualityDetection=0)
    return len(r.get("FaceInfos") or [])


def detect_faces(image_bytes: bytes) -> list[dict]:
    """检测人脸并带上姿态、清晰度等信息，用来从一堆候选里挑"正脸、清楚"的那几张。
       返回 [{w,h,yaw,pitch,roll,quality,sharp,bright}]。"""
    if MOCK:
        return [{"x": 0, "y": 0, "w": 200, "h": 200, "yaw": 0, "pitch": 0, "roll": 0, "quality": 80, "sharp": 80, "bright": 60}]
    r = _call("DetectFace", Image=base64.b64encode(image_bytes).decode(), MaxFaceNum=10,
              NeedFaceAttributes=1, NeedQualityDetection=1)
    out = []
    for fi in r.get("FaceInfos") or []:
        attr = fi.get("FaceAttributesInfo") or {}
        q = fi.get("FaceQualityInfo") or {}
        out.append({
            "x": fi.get("X", 0), "y": fi.get("Y", 0),
            "w": fi.get("Width", 0), "h": fi.get("Height", 0),
            "yaw": abs(attr.get("Yaw", 0) or 0), "pitch": abs(attr.get("Pitch", 0) or 0),
            "roll": abs(attr.get("Roll", 0) or 0),
            "quality": q.get("Score", 0) or 0, "sharp": q.get("Sharpness", 0) or 0,
            "bright": q.get("Brightness", 0) or 0,
        })
    return out


def frontal_score(f: dict) -> float:
    """给一张脸打分：越正、越大、越清楚，分越高。用来在合格的照片里排序。"""
    pose = (f["yaw"] + f["pitch"] + f["roll"]) / 3.0
    return f["quality"] * 0.5 + f["sharp"] * 0.3 + min(f["w"], 260) * 0.25 - pose * 2.0


# 两档标准：优先只要"正脸"，实在找不到才退而求其次。
# 角度按腾讯自己的建议（推荐 ±30° 以内）来定，不要卡得比人眼还苛刻：
# 实测 17~18° 的照片其实就是孩子正对镜头、只是低头看了一眼手里的东西，完全能用。
FRONT_STRICT = {"yaw": 25, "pitch": 25, "roll": 20, "quality": 50, "sharp": 40, "w": 70}
FRONT_LOOSE = {"yaw": 35, "pitch": 32, "roll": 28, "quality": 30, "sharp": 22, "w": 50}


def _pass(f: dict, bar: dict) -> bool:
    return (f["yaw"] <= bar["yaw"] and f["pitch"] <= bar["pitch"] and f["roll"] <= bar["roll"]
            and f["quality"] >= bar["quality"] and f["sharp"] >= bar["sharp"] and f["w"] >= bar["w"])


def face_grade(f: dict) -> str:
    """front = 完整正脸，可以放心当底照；ok = 勉强能用；bad = 不要。"""
    if _pass(f, FRONT_STRICT):
        return "front"
    if _pass(f, FRONT_LOOSE):
        return "ok"
    return "bad"


def why_not_front(f: dict) -> str:
    """说明这张为什么算不上完全正脸，给老师看。"""
    r = []
    if f["yaw"] > FRONT_STRICT["yaw"]:
        r.append(f"左右偏头 {f['yaw']:.0f}°")
    if f["pitch"] > FRONT_STRICT["pitch"]:
        r.append(f"抬/低头 {f['pitch']:.0f}°")
    if f["roll"] > FRONT_STRICT["roll"]:
        r.append(f"歪头 {f['roll']:.0f}°")
    if f["quality"] < FRONT_STRICT["quality"]:
        r.append(f"画质 {f['quality']:.0f}")
    if f["sharp"] < FRONT_STRICT["sharp"]:
        r.append(f"清晰度 {f['sharp']:.0f}")
    if f["w"] < FRONT_STRICT["w"]:
        r.append(f"脸太小 {f['w']}px")
    return "、".join(r) or "尚可"


def is_usable_front(f: dict) -> bool:
    return face_grade(f) != "bad"

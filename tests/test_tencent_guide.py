"""Offline documentation contract checks; no real credentials or cloud calls."""
import ast
import json
import re
from pathlib import Path

root = Path(__file__).resolve().parents[1]
guide = (root / "docs/tencent-face-guide.md").read_text(encoding="utf-8")
html = (root / "app/static/index.html").read_text(encoding="utf-8")
face = ast.parse((root / "app/face_client.py").read_text(encoding="utf-8"))
actions = {
    node.args[0].value
    for node in ast.walk(face)
    if isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "_call"
    and node.args
    and isinstance(node.args[0], ast.Constant)
    and isinstance(node.args[0].value, str)
}
blocks = re.findall(r"```json\s*([\s\S]*?)```", guide)
assert len(blocks) == 1
policy = json.loads(blocks[0])
assert policy["version"] == "2.0"
assert len(policy["statement"]) == 1
statement = policy["statement"][0]
assert statement["effect"] == "allow"
assert statement["resource"] == ["*"]
assert set(statement["action"]) == {"name/iai:" + action for action in actions}
assert len(actions) == 10
assert "name/iai:DeleteGroup" not in statement["action"]
for caption in [
    "可选：使用自己的腾讯云人脸识别", "启用人脸识别", "保存模型设置",
    "测试连接", "花名册·人脸", "从幼师口袋同步名单", "查云端已有",
    "把新照片入库", "开始识别", "从班级相册选照片", "切换班级",
    "删除本机已保存的腾讯云凭据",
]:
    assert caption in guide, caption
    assert caption in html, caption
assert "QcloudIAIFullAccess" not in guide
assert "QcloudFRFullAccess" in guide
assert "resource: [\"*\"]" in guide
assert "不是按某一位老师隔离权限" in guide
assert "不能把本机照片消失当作云端已清空" in guide
for segment in ["867/54248", "598/13674", "598/37140", "867/17640", "867/35076", "867/45012"]:
    assert segment in guide
assert not re.search(r"AKID[A-Za-z0-9]{12,}|sk-[A-Za-z0-9]{20,}|BEGIN .*PRIVATE KEY", guide)
print("PASS: policy equals all 10 current face API calls; no DeleteGroup privilege; UI captions and official links match; shared-cloud and deletion limitations documented; no credential-shaped values. Offline only.")

/* Community configuration: secrets are entered locally and never read back. */
const CommunitySettings={
  value:null,loading:null,saving:false,
  fields:{provider:'cfgProvider',base_url:'cfgBaseUrl',model:'cfgModel',thinking:'cfgThinking',tencent_region:'cfgTencentRegion'},
  async load(){
    if(this.loading)return this.loading;
    this.loading=(async()=>{
      try{
        const value=await api('/api/community/settings');
        this.render(value);return value;
      }catch(e){$('#cfgTip').textContent='读取设置失败：'+e.message;return null;}
    })();
    try{return await this.loading;}finally{this.loading=null;}
  },
  render(value){
    this.value=value||{};
    const env=new Set(this.value.env_overrides||[]);
    Object.keys(this.fields).forEach(key=>{
      const el=$('#'+this.fields[key]);el.value=this.value[key]|| (key==='thinking'?'default':'');el.disabled=env.has(key);
    });
    $('#cfgFaceEnabled').checked=!!this.value.face_enabled;$('#cfgFaceEnabled').disabled=env.has('face_enabled');
    $('#cfgApiKeyStatus').textContent=this.value.api_key_set?'已有 API 密钥；留空保留。':'尚未配置 API 密钥。';
    $('#cfgTencentIdStatus').textContent=this.value.tencent_id_set?'已配置 SecretId；留空保留。':'尚未配置 SecretId。';
    $('#cfgTencentKeyStatus').textContent=this.value.tencent_key_set?'已配置 SecretKey；留空保留。':'尚未配置 SecretKey。';
    $('#cfgApiKey').disabled=env.has('api_key');$('#cfgClearApiKey').disabled=env.has('api_key');
    $('#cfgTencentId').disabled=env.has('tencent_id');$('#cfgTencentKey').disabled=env.has('tencent_key');
    $('#cfgClearTencent').disabled=env.has('tencent_id')||env.has('tencent_key');
    $('#cfgEnvOverrides').hidden=!env.size;
    $('#cfgEnvOverrides').textContent=env.size?'以下设置由启动环境提供，修改后需重启程序：'+Array.from(env).join('、'):'';
    $('#communityDataDir').textContent=this.value.data_dir||'使用本机社区版数据目录';
    const face=!!this.value.face_enabled;
    $('#btnStart').textContent=face?'开始识别':'整理照片并选择幼儿';
    $('#faceStat').textContent=face?'已启用自己的腾讯云服务，可测试连接。':'当前关闭，可以手动选择幼儿。';
    $('#btnFaceTest').disabled=!face;
  },
  async save(){
    if(this.saving)return;
    const env=new Set((this.value&&this.value.env_overrides)||[]),body={};
    Object.keys(this.fields).forEach(key=>{if(!env.has(key))body[key]=$('#'+this.fields[key]).value.trim();});
    if(!env.has('face_enabled'))body.face_enabled=$('#cfgFaceEnabled').checked;
    if(!env.has('api_key')){body.clear_api_key=$('#cfgClearApiKey').checked;body.api_key=body.clear_api_key?'':$('#cfgApiKey').value.trim();}
    if(!env.has('tencent_id'))body.tencent_id=$('#cfgTencentId').value.trim();
    if(!env.has('tencent_key'))body.tencent_key=$('#cfgTencentKey').value.trim();
    if(!env.has('tencent_id')&&!env.has('tencent_key')){body.clear_tencent=$('#cfgClearTencent').checked;if(body.clear_tencent){body.tencent_id='';body.tencent_key='';}}
    this.saving=true;$('#cfgSave').disabled=true;$('#cfgTip').textContent='正在保存…';
    try{
      const result=await api('/api/community/settings',{method:'POST',json:body});
      if(result&&result.error)throw new Error(result.error);
      ['cfgApiKey','cfgTencentId','cfgTencentKey'].forEach(id=>{$('#'+id).value='';});
      $('#cfgClearApiKey').checked=false;$('#cfgClearTencent').checked=false;
      await this.load();
      $('#cfgTip').textContent='设置已保存。密钥输入框已清空，后续请求使用本机保存的配置。';
      if(typeof ObservationAI!=='undefined')ObservationAI.load();
      toast('模型设置已保存','ok');
    }catch(e){$('#cfgTip').textContent='保存失败：'+e.message;}
    finally{this.saving=false;$('#cfgSave').disabled=false;}
  }
};

const CommunityResources={
  loading:false,importing:false,
  render(result){
    const data=result||{},files=Array.isArray(data.files)?data.files:[];
    $('#resourceStatus').textContent=data.message||`本机资料 ${files.length} 份。`;
    $('#resourceFiles').innerHTML=files.length?'<details><summary>查看本机资料文件</summary><ul>'+files.slice(0,40).map(file=>'<li>'+esc(file.path)+' <span class="hint">'+Math.ceil((Number(file.bytes)||0)/1024)+' KB</span></li>').join('')+'</ul>'+(files.length>40?'<p>其余 '+(files.length-40)+' 份文件已导入。</p>':'')+'</details>':'尚未导入资料，可继续使用观察和随手记功能。';
  },
  async load(){
    if(this.loading)return;
    this.loading=true;
    try{this.render(await api('/api/resources'));}
    catch(e){$('#resourceStatus').textContent='读取资料状态失败：'+e.message;}
    finally{this.loading=false;}
  },
  async importZip(){
    if(this.importing)return;
    const file=$('#resourceZip').files[0];
    if(!file){$('#resourceStatus').textContent='请先选择 ZIP 资料包。';return;}
    if(!/\.zip$/i.test(file.name)){ $('#resourceStatus').textContent='请选择 .zip 格式的资料包。';return; }
    const form=new FormData();form.append('file',file);
    this.importing=true;$('#resourceImport').disabled=true;$('#resourceStatus').textContent='正在校验并导入资料…';
    try{
      const result=await api('/api/resources/import',{method:'POST',form});
      this.render(result);$('#resourceZip').value='';
      $('#resourceStatus').textContent='资料导入成功。知识库会使用本机导入内容。';
      toast('资料包已导入本机','ok');
    }catch(e){$('#resourceStatus').textContent='导入失败：'+e.message;}
    finally{this.importing=false;$('#resourceImport').disabled=false;}
  },

};

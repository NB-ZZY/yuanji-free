/* Real progress: completed work and received answer characters, never invented %. */
const ObservationAI={
  info:null, selected:{},
  labels:{off:'关闭思考',low:'低',high:'高',max:'最高',default:'模型默认'},
  async load(){
    try{
      this.info=await api('/api/ai/effort');
      const el=document.getElementById('obsThinking');
      if(el) el.innerHTML=this.options(this.info.selected);
      if(el) el.disabled=!this.info.levels.length;
      const hint=document.getElementById('obsThinkingHint');
      if(hint) hint.textContent=this.info.model+' · '+this.info.hint;
      if(S.batch) renderBatch(S.batch);
    }catch(e){
      const hint=document.getElementById('obsThinkingHint');
      if(hint) hint.textContent='思考设置读取失败，请重新打开此页。';
    }
  },
  itemInfo(item){return (item&&item.ai_effort_options)||this.info;},
  options(value,info){
    info=info||this.info;
    const levels=(info&&info.levels.length)?info.levels:['default'];
    return levels.map(k=>`<option value="${k}" ${k===value?'selected':''}>${this.labels[k]}</option>`).join('');
  },
  effort(item){
    const info=this.itemInfo(item);
    const chosen=this.selected[(item&&item.id)]||(item&&item.ai_effort)||(info&&info.selected)||'';
    if(!info)return chosen;
    const levels=info.levels.length?info.levels:['default'];
    return levels.includes(chosen)?chosen:(info.default||levels[0]);
  },
  async change(value,id){
    const old=id?this.selected[id]:(this.info&&this.info.selected);
    if(id) this.selected[id]=value;
    try{
      if(id){
        const result=await api('/api/item/'+id+'/effort',{method:'POST',json:{effort:value}});
        const item=(S.batch&&S.batch.items||[]).find(i=>i.id===id);
        if(item){item.ai_effort=value;if(result.options)item.ai_effort_options=result.options;}
      }
      else{
        this.info=await api('/api/ai/effort',{method:'POST',json:{effort:value}});
        const el=document.getElementById('obsThinking'); if(el) el.value=value;
        // Batch selection is explicit; unrelated records retain their own setting.
        for(const sid of selSet()){
          const item=(S.batch&&S.batch.items||[]).find(i=>i.id===sid);
          const info=this.itemInfo(item);
          if(info&&(info.levels.length?info.levels:['default']).includes(value))this.selected[sid]=value;
        }
      }
      if(S.batch) renderBatch(S.batch);
    }catch(e){
      if(id){ if(old) this.selected[id]=old; else delete this.selected[id]; }
      toast(e.message,'err'); if(S.batch) renderBatch(S.batch);
    }
  },
  picker(item){
    const info=this.itemInfo(item);
    return `<label class="hint" title="${esc(info?info.model+' · '+info.hint:'正在读取模型设置')}" style="display:inline-flex;gap:6px;align-items:center">思考程度
      <select class="inp" aria-label="此条记录思考程度" style="width:110px;padding:4px 8px" ${item.ai_status==='writing'||!(info&&info.levels.length)?'disabled':''}
       onchange="ObservationAI.change(this.value,${item.id})">${this.options(this.effort(item),info)}</select></label>`;
  },
  batchPicker(){
    return `<label class="hint" style="display:inline-flex;gap:6px;align-items:center">思考程度
      <select class="inp" aria-label="批量思考程度" style="width:110px;padding:4px 8px" ${!(this.info&&this.info.levels.length)?'disabled':''}
        onchange="ObservationAI.change(this.value)">${this.options((this.info&&this.info.selected))}</select></label>`;
  },
  progressHtml(progress,active=false){
    if(!active && (!progress||progress.status==='done')) return '';
    const p=progress||{};
    const phase={queued:'排队中',preparing:'正在准备',waiting:'等待模型响应',thinking:'模型正在思考',receiving:'正在接收正文',retrying:'接口繁忙，稍后重试',checking:'正在核对',error:'请查看错误提示',done:'已完成'}[p.phase]||'正在准备';
    const count=p.received_chars?` · 已接收正文 ${p.received_chars} 字符`:'';
    const steps=p.total?` · 已完成 ${p.completed}/${p.total} 步`:'';
    return `<div class="ai-live" role="status"><div class="hint">${esc(p.stage||'准备生成')} · ${phase}${steps}${count}</div>
      <progress aria-label="已完成任务步骤" ${p.total?`value="${p.completed||0}" max="${p.total}"`:''}></progress></div>`;
  },
  itemProgress(item){return `<div id="obsProgress${item.id}">${this.progressHtml(item.ai_progress,item.ai_status==='writing')}</div>`;},
  batchProgress(items){
    const tasks=items.filter(i=>i.ai_progress);
    if(!items.some(i=>i.ai_status==='writing')) return '';
    const done=tasks.filter(i=>i.ai_progress.status==='done').length;
    const errors=tasks.filter(i=>i.ai_progress.status==='error').length;
    return `<div class="ai-live" role="status"><div class="hint">本批次写作：已完成 ${done} 条${errors?'，失败 '+errors+' 条':''}，处理中 ${items.filter(i=>i.ai_status==='writing').length} 条</div>
      <progress aria-label="本批次完成记录数" value="${done+errors}" max="${Math.max(tasks.length,1)}"></progress></div>`;
  },
  watch(id){
    let alive=true;
    const tick=async()=>{
      try{
        const p=await api('/api/item/'+id+'/ai-progress'); if(!alive) return;
        for(const target of ['obsProgress'+id,'rewriteProgress']){
          const el=document.getElementById(target); if(el) el.innerHTML=this.progressHtml(p,true);
        }
      }catch(e){}
    };
    tick(); const timer=setInterval(tick,900);
    return ()=>{alive=false;clearInterval(timer)};
  }
};
window.addEventListener('DOMContentLoaded',()=>ObservationAI.load());

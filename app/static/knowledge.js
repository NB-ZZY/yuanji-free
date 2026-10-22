/* 本地只读知识库。所有来源、原文和用户检索内容均按文本转义。 */
const Knowledge = {
  request: 0,
  async open() {
    const age=typeof S!=='undefined'&&S&&S.cls&&Number(S.cls.level)===128?'24—36个月':'';
    modal(`<h2>内置知识库</h2><p class="muted">按领域和年龄查原文。知识依据帮助理解行为，不能代替现场记录，也不能据此机械判定达标。</p>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <select id="knowledgeDomain" aria-label="知识库领域" onchange="Knowledge.search()"><option value="">全部领域</option>${['健康领域','语言领域','社会领域','科学领域','艺术领域','动作与习惯','情感与社会','认知与探索','语言与沟通'].map(x=>`<option>${esc(x)}</option>`).join('')}</select>
        <select id="knowledgeAge" aria-label="知识库年龄" onchange="Knowledge.search()"><option value="">全部年龄</option>${['3-4岁','4-5岁','5-6岁','24—36个月','未分年龄'].map(x=>`<option${x===age?' selected':''}>${esc(x)}</option>`).join('')}</select>
        <select id="knowledgeSource" aria-label="知识库来源" onchange="Knowledge.search()"><option value="">全部来源</option><option value="official_guide">教育部指南原文</option><option value="provided_reference">用户提供的慧园通观察指引</option><option value="core_indicator">六领域原始核心经验表</option><option value="local_summary">园本整理（待核验）</option><option value="nursery_development">托育24—36个月发展要点（用户原件）</option><option value="nursery_indicator">官网托育体系24—36个月指标</option></select>
        <input id="knowledgeQuery" aria-label="知识库关键词" placeholder="输入关键词，如合作、双脚、倾听" maxlength="100" onkeydown="if(event.key==='Enter')Knowledge.search()">
        <button class="btn" onclick="Knowledge.search()">查找</button>
      </div><div id="knowledgeResults" style="margin-top:14px;max-height:55vh;overflow:auto">正在读取…</div>
      <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn" onclick="closeModal()">关闭</button></div>`);
    await this.search();
  },
  async search() {
    const box=document.getElementById('knowledgeResults'); if(!box)return;
    const serial=++this.request;
    const params=new URLSearchParams({domain:document.getElementById('knowledgeDomain').value,age:document.getElementById('knowledgeAge').value,q:document.getElementById('knowledgeQuery').value,source:document.getElementById('knowledgeSource').value});
    box.textContent='正在读取…';
    try {
      const data=await api('/api/knowledge?'+params);
      if(serial!==this.request||!box.isConnected)return;
      // 接口返回完整筛选目录；保留老师正在查看的领域、月龄和来源。
      for(const [id,values,title] of [['knowledgeDomain',data.domains,'全部领域'],['knowledgeAge',data.ages,'全部年龄'],['knowledgeSource',data.sources,'全部来源']]){
        const select=document.getElementById(id);if(!select||!Array.isArray(values))continue;
        const selected=select.value;
        select.innerHTML=`<option value="">${title}</option>`+values.map(v=>typeof v==='string'?{kind:v,name:v}:v)
          .filter(v=>v&&typeof v.kind==='string'&&typeof v.name==='string')
          .map(v=>`<option value="${esc(v.kind)}">${esc(v.name)}</option>`).join('');
        select.value=selected;
      }
      box.innerHTML=`<p class="muted">共 ${Number(data.total)||0} 条${data.total>data.items.length?`，显示前 ${data.items.length} 条，请缩小查询范围`:''}。园本整理资料已单独标注，原出处核验前不用于自动分析。</p>`+
        (data.items.length?data.items.map(c=>this.chunkHtml(c)).join(''):'<p>没有匹配条文，请调整关键词或年龄。</p>');
    } catch(e) { if(serial===this.request&&box.isConnected)box.textContent='知识库读取失败：'+e.message; }
  },
  chunkHtml(c) {
    const sourceUrl=/^https:\/\/www\.moe\.gov\.cn\//.test(c.source_url||'')?c.source_url:'';
    return `<details style="border:1px solid #dfe7e5;border-radius:10px;padding:10px 12px;margin:8px 0;background:#fff">
      <summary style="cursor:pointer;line-height:1.6">${esc(c.heading||'')} · ${esc(c.age_band||'')} ${c.enabled_for_analysis===false?'（整理资料，未核验）':''}</summary>
      <div class="muted" style="margin:8px 0">${esc(c.source||'')} · ${esc(c.location||'')}${sourceUrl?` · <a href="${esc(sourceUrl)}" target="_blank" rel="noopener noreferrer">查看官方出处</a>`:''}</div>
      ${c.age_note||c.source_note?`<p class="muted">${esc(c.age_note||c.source_note)}</p>`:''}
      <div style="white-space:pre-wrap;line-height:1.85">${esc(c.text||'')}</div></details>`;
  },
  evidenceHtml(table) {
    if(!table||typeof table!=='object')return '';
    const items=Array.isArray(table['分析依据'])?table['分析依据']:[];
    const outdated=items.length && (table['依据对应实录']!==table['观察内容']||table['依据对应分析']!==table['分析']);
    if(!items.length)return '<div class="muted" style="margin:8px 0">本条分析暂无可核验的知识库引用；请结合实录审阅，不将推测作为能力结论。</div>';
    return `<details style="margin:12px 0"><summary style="cursor:pointer">${outdated?'内容已修改，请重写分析更新依据':'查看本条分析的知识依据'}（${items.length} 条）</summary>
      <p class="muted">${outdated?'以下为旧稿引用，仅供对照，尚未核验是否适用于修改后的内容。':'以下是分析引用的知识条文；幼儿是否有相应表现，仍须回到观察实录判断。'}</p>${items.map(c=>this.chunkHtml(c)).join('')}</details>`;
  }
};

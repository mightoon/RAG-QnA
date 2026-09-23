/* 配置页主控制器：Tab 切换 / 离开提醒
   保存由各模块卡片内的按钮各自负责（只写本模块的键），
   页面不再提供「保存全部」——避免一次点击改写所有模块的配置 */
(function () {
  'use strict';
  const qs = (s, r) => (r || document).querySelector(s);
  const qsa = (s, r) => Array.from((r || document).querySelectorAll(s));

  let currentTab = 'model';

  function switchTab(tab) {
    /* 切走模型页时先收起那颗编辑弹窗：它挂在 document.body 上（不随面板刷新消失），
       留着会盖在刚切过去的这一页上 —— 弹窗里编的是模型页的东西，看不见它就读不懂 */
    if (currentTab === 'model' && tab !== 'model'
        && window.ConfigUI && ConfigUI.closeEditor) ConfigUI.closeEditor();
    currentTab = tab;
    qsa('#config-tabs .tab').forEach(t => {
      const on = t.dataset.ctab === tab;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    qsa('[data-ctab-panel]').forEach(p => {
      p.classList.toggle('hidden', p.dataset.ctabPanel !== tab);
    });
    Partials.refreshPiece('panel_' + tab);
  }

  function init() {
    const page = qs('#config-page');
    if (!page) return;
    if (!ConfigData.load()) {
      page.innerHTML = '<div class="error-box"><span>配置加载失败</span>' +
        '<button class="btn btn-secondary btn-sm" onclick="location.reload()">重试</button></div>';
      return;
    }
    ConfigUI.setup();
    qsa('#config-tabs .tab').forEach(t => {
      t.addEventListener('click', () => switchTab(t.dataset.ctab));
    });
    switchTab('model');

    // 仍有模块未保存时，离开页面给一次提醒
    window.addEventListener('beforeunload', e => {
      if (ConfigData.state.dirty) { e.preventDefault(); e.returnValue = ''; }
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();

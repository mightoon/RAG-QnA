/* 知识库任务追踪：轮询活动任务，驱动徽标与列表增量刷新 */
(function () {
  'use strict';
  const ACTIVE = ['queued', 'parsing', 'embedding', 'uploading', 'indexing', 'retrying'];
  let ctx = null;
  let timer = null;
  let lastActive = 0;

  async function poll() {
    try {
      const tasks = await API.getData('/api/ingest/tasks?limit=50');
      const list = Array.isArray(tasks) ? tasks : (tasks.tasks || []);
      const active = list.filter(t => ACTIVE.includes(t.status));
      if (ctx && ctx.updateTaskBadges) ctx.updateTaskBadges(active.length);
      // 活动任务数变化或存在进行中任务时刷新当前视图
      if (ctx && (active.length !== lastActive || (active.length && ctx.state.tab === 'tasks'))) {
        ctx.refreshAll();
      }
      lastActive = active.length;
    } catch (e) {
      // 静默失败，下轮重试
    }
  }

  function start() {
    stop();
    poll();
    timer = setInterval(poll, 4000);
  }
  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
  }

  function init(context) {
    ctx = context || {};
    start();
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) stop(); else start();
    });
  }

  window.KnowledgeTasks = { init, poll };
})();

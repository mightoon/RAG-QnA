/* 知识库任务追踪：轮询活动任务，驱动徽标与列表增量刷新 */
(function () {
  'use strict';
  // 与后端 IngestStatus 的真实取值对齐（rag/models.py）。
  // 历史缺陷：这里原先是 queued/uploading/indexing —— 这三个值后端从不产生，
  // 而真实的活动态 pending/chunking/writing 又不在表里，于是徽标恒为 0、
  // "进行中"过滤永远筛不出东西（见 routes._ACTIVE_TASK_STATUS 的同一份口径）。
  const ACTIVE = ['pending', 'parsing', 'chunking', 'embedding', 'writing', 'retrying'];
  let ctx = null;
  let timer = null;
  let lastActive = 0;

  async function poll() {
    try {
      const tasks = await API.getData('/api/tasks?limit=50');
      const list = Array.isArray(tasks) ? tasks : (tasks.tasks || []);
      const active = list.filter(t => ACTIVE.includes(t.status));
      if (ctx && ctx.updateTaskBadges) ctx.updateTaskBadges(active.length);
      /* 刷新条件：活动任务数变化 **或"仍有任务在跑"**。
         原来的条件只有"数量变化 || 当前在任务页"，于是停在文档页时：
         任务从 pending→chunking→writing 一路走，活动数始终是 1（没变化）→
         列表一次都不刷新 → 用户看到状态一直卡在"排队中"，直到任务结束
         （活动数 1→0 才触发刷新）突然跳到"已完成"。
         改成"只要还有活动任务就每轮刷新"，文档列表才能看到阶段推进。 */
      if (ctx && (active.length !== lastActive || active.length > 0)) {
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

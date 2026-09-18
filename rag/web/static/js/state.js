/* 全局状态容器：跨页面命名空间隔离 + 会话级偏好本地持久化 */
window.State = class StateClass {
  constructor(page) {
    this.page = page;
    this._data = {};
    this._listeners = {};
  }
  get(key, fallback) { return this._data[key] !== undefined ? this._data[key] : fallback; }
  set(key, value) {
    this._data[key] = value;
    (this._listeners[key] || []).forEach(function (fn) {
      try { fn(value); } catch (e) { console.error(e); }
    });
    return value;
  }
  on(key, fn) {
    (this._listeners[key] = this._listeners[key] || []).push(fn);
  }
  persist(key, value) {
    try {
      if (value === null || value === undefined) localStorage.removeItem(key);
      else localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* 隐私模式降级静默 */ }
    return this.set(key, value);
  }
  restore(key, fallback) {
    try {
      var raw = localStorage.getItem(key);
      if (raw !== null) {
        var v = JSON.parse(raw);
        this._data[key] = v;
        return v;
      }
    } catch (e) { /* ignore */ }
    return fallback;
  }
};

/* 供调试：state = new State('chat') 每页由模块初始化时创建 */

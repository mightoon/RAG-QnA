"""UI 烟测：
A. 空配置（LLM/Embedding 未配置、外部依赖不可达）下服务可启动并降级运行
B. 配置页保存 → 热重建生效（无需重启）
C. 演示模式（noconnection）基础页面/接口
运行前自动备份 customer/customer_config.yaml，结束后恢复。
"""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = ROOT / "customer" / "customer_config.yaml"
BAK = ROOT / "customer" / "customer_config.yaml.smoke_bak"

shutil.copyfile(CFG, BAK)
try:
    # ── A. 空配置启动（不设 noconnection，走真实降级路径）──
    from fastapi.testclient import TestClient
    from rag.api.app import create_app_from_env

    print("=== A. 空配置启动（依赖自动降级） ===")
    app = create_app_from_env()
    with TestClient(app) as c:
        t = c.post('/api/auth/token', json={'user_id': 'u01', 'roles': ['admin']}).json()
        h = {'Authorization': 'Bearer ' + (t.get('token') or t.get('access_token'))}
        r = c.get('/api/health', headers=h)
        print('health', r.status_code, r.json().get('degraded'))
        for path in ['/chat', '/knowledge', '/config']:
            r = c.get(path, headers=h)
            print(path, r.status_code, len(r.text))
        r = c.get('/api/health/full', headers=h)
        print('health/full', r.status_code)

        # ── B. 配置保存 → 热重建生效 ──
        print("=== B. 保存配置 → 热应用 ===")
        payload = {
            'retrieval': {'rerankEnabled': True,
                          'rerankModel': 'BAAI/bge-reranker-v2-m3',
                          'rerankThreshold': 0.35},
            'permissionMappings': [{'role': 'admin', 'collections': ['*'], 'is_admin': True}],
            'modelProviders': [{
                'key': 'llm', 'endpoint': 'http://127.0.0.1:9/v1',
                'configParams': [
                    {'key': 'model', 'value': 'test-model', 'type': 'str'},
                    {'key': 'api_key', 'value': 'sk-x', 'type': 'str'},
                    {'key': 'temperature', 'value': '0.5', 'type': 'float'},
                    {'key': 'max_tokens', 'value': '1024', 'type': 'int'},
                ]}],
            'serviceGroups': [{
                'key': 'mysql_meta',
                'configParams': [
                    {'key': 'host', 'value': '127.0.0.1', 'type': 'str'},
                    {'key': 'port', 'value': '3306', 'type': 'int'},
                ]}],
            'specialGroups': [],
        }
        r = c.post('/api/admin/config', json=payload, headers=h)
        body = r.json()
        print('save', r.status_code, 'applied=', body.get('applied'),
              'degraded_keys=', sorted((body.get('degraded') or {}).keys()))
        # 热生效验证：新容器 LLM 已配置（不再是 mock 降级）
        r = c.get('/api/health', headers=h)
        deg = r.json().get('degraded') or {}
        print('after-rebuild degraded has llm?', 'llm' in deg)
        # YAML 写回验证
        text = CFG.read_text(encoding='utf-8')
        print('yaml has model?', 'test-model' in text,
              '| rerank_model?', 'bge-reranker-v2-m3' in text,
              '| port int?', 'port: 3306' in text)
        # 连接测试（LLM 端点直测，127.0.0.1:9 不可达 → 失败但接口正常）
        r = c.post('/api/admin/health/test',
                   json={'kind': 'llm', 'endpoint': 'http://127.0.0.1:9/v1'}, headers=h)
        print('health/test llm', r.status_code, r.json().get('online'),
              r.json().get('message', '')[:40])

    # ── C. 演示模式 ──
    print("=== C. 演示模式（noconnection） ===")
    os.environ['RAG_NOCONNECTION'] = '1'
    app2 = create_app_from_env()
    with TestClient(app2) as c:
        t = c.post('/api/auth/token', json={'user_id': 'u01', 'roles': ['admin']}).json()
        h = {'Authorization': 'Bearer ' + (t.get('token') or t.get('access_token'))}
        for path in ['/api/ui/piece/knowledge/stats_cards',
                     '/api/ui/piece/knowledge/docs_tab?page=1&size=10',
                     '/api/ui/piece/knowledge/trash_tab',
                     '/api/ui/piece/knowledge/tasks_tab',
                     '/api/ui/piece/chat/sessions',
                     '/api/documents',
                     '/api/admin/config']:
            r = c.get(path, headers=h)
            print(path, r.status_code, r.text[:60].replace('\n', ' '))
        r = c.get('/static/js/partials.js')
        print('static', r.status_code)
        for method, path in [('post', '/api/documents/nope/restore'),
                             ('delete', '/api/documents/nope/permanent'),
                             ('post', '/api/tasks/nope/retry')]:
            r = getattr(c, method)(path, headers=h)
            print(method, path, r.status_code)
finally:
    shutil.copyfile(BAK, CFG)
    BAK.unlink()
    print('config restored')

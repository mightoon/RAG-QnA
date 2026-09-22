"""UI 烟测：
A. 空配置（LLM/Embedding 未配置、外部依赖不可达）下服务可启动并降级运行
B. 配置页保存 → 热重建生效（无需重启）
C. 演示模式（noconnection）基础页面/接口
运行前自动备份 customer/customer_config.yaml，结束后恢复。
"""
import os
import re
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
        # 模型段改走模型库接口：/api/admin/config 会拒收 modelProviders 与
        # retrieval.rerank* 镜像字段（见 routes._yaml_update_from_payload），
        # 顶层字段由 active 条目在加载时重写
        r = c.post('/api/admin/model-library/upsert', json={
            'section': 'llm', 'endpoint': 'http://127.0.0.1:9/v1',
            'modelIds': ['test-model'],
            'configParams': [
                {'key': 'display_name', 'value': 'smoke-llm', 'type': 'str'},
                {'key': 'model', 'value': 'test-model', 'type': 'str'},
                {'key': 'api_key', 'value': 'sk-x', 'type': 'str'},
                {'key': 'temperature', 'value': '0.5', 'type': 'float'},
                {'key': 'max_tokens', 'value': '1024', 'type': 'int'},
            ], 'verified': True, 'activate': True}, headers=h)
        print('model upsert', r.status_code, r.json().get('ok'))
        payload = {
            'retrieval': {'rerankEnabled': True, 'rerankThreshold': 0.35},
            'permissionMappings': [{'role': 'admin', 'collections': ['*'], 'is_admin': True}],
            'serviceGroups': [{
                'key': 'meta',
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

        # ── 重排模型库：多套配置并存 + 切 active + 删除 ──
        # 重排是本机 Cross-Encoder 权重（无地址/凭据），库挂在 retrieval 上
        # （见 routes._RerankStore / models._sync_rerank_library）
        def _rerank_upsert(name, device):
            # 「模型路径」= 权重所在目录、「模型ID」= 目录下的子目录名：两行分开存，
            # 徽标与模型ID 都不带路径前缀（见 models.resolve_rerank_model_path）
            return c.post('/api/admin/model-library/upsert', json={
                'section': 'rerank',
                'modelIds': ['bge-reranker-v2-m3'],
                'configParams': [
                    {'key': 'display_name', 'value': name, 'type': 'str'},
                    {'key': 'model_dir', 'value': 'models', 'type': 'str'},
                    {'key': 'model', 'value': 'bge-reranker-v2-m3', 'type': 'str'},
                    {'key': 'device', 'value': device, 'type': 'str'},
                ], 'verified': True, 'activate': False}, headers=h)

        # 「获取模型」：按表单里的「模型路径」列目录，候选只回目录名
        r = c.post('/api/admin/model-library/list-models',
                   json={'kind': 'rerank',
                         'params': [{'key': 'model_dir', 'value': 'models',
                                     'type': 'str'}]}, headers=h)
        picked = r.json().get('models') or []
        print('rerank list-models', r.status_code, picked[:2],
              '| bare(no path)?', bool(picked) and all('/' not in m for m in picked))
        # 「测试模型」：模型路径 + 模型ID 拼出的目录在才算过
        r = c.post('/api/admin/health/test',
                   json={'kind': 'rerank', 'model': 'bge-reranker-base',
                         'params': [{'key': 'model_dir', 'value': 'models',
                                     'type': 'str'}]}, headers=h)
        print('rerank health/test', r.status_code, r.json().get('online'),
              (r.json().get('message') or '')[:60])
        # 「模型路径/API」也接受远程重排服务地址（同一栏两义，见 models.is_http_url）：
        # 列候选只回一句说明（远程不列本地权重）；「测试模型」真打它的 /rerank ——
        # 给一个不可达端口，必须如实判失败，且地址要补成 …/v1/rerank（与运行期同一个
        # 补全函数），不能只看"路径存不存在"就报通过
        url_params = [{'key': 'model_dir', 'value': 'http://127.0.0.1:9/v1',
                       'type': 'str'}]
        r = c.post('/api/admin/model-library/list-models',
                   json={'kind': 'rerank', 'params': url_params}, headers=h)
        print('rerank list-models (url)', r.status_code, r.json().get('ok'),
              (r.json().get('message') or '')[:50])
        r = c.post('/api/admin/health/test',
                   json={'kind': 'rerank', 'model': 'bge-reranker-base',
                         'params': url_params}, headers=h)
        _url_msg = r.json().get('message') or ''
        print('rerank health/test (url)', r.status_code, r.json().get('online'),
              _url_msg[:60])
        assert r.json().get('online') is False, '不可达的远程重排地址必须判失败'
        assert '/rerank' in _url_msg, '地址应补成 <地址>/rerank 再打'
        # 「模型路径/API」必填（后端校验，前端也拦）：空着等于这条配置谁也重排不了
        r = c.post('/api/admin/model-library/upsert', json={
            'section': 'rerank', 'modelIds': ['bge-reranker-base'],
            'configParams': [
                {'key': 'display_name', 'value': 'smoke-rerank-nodir',
                 'type': 'str'},
                {'key': 'model_dir', 'value': '', 'type': 'str'},
                {'key': 'model', 'value': 'bge-reranker-base', 'type': 'str'},
            ], 'verified': True}, headers=h)
        print('rerank upsert w/o model_dir', r.status_code,
              (r.json().get('detail') or '')[:40])
        assert r.status_code == 400, '缺「模型路径/API」应被拒收'

        first = _rerank_upsert('smoke-rerank-cpu', 'cpu').json()
        r2 = _rerank_upsert('smoke-rerank-gpu', 'cuda')
        second = r2.json()
        entries = (second.get('library') or {}).get('entries') or []
        cpu_id = next(e['id'] for e in entries
                      if e.get('displayName') == 'smoke-rerank-cpu')
        gpu_id = next(e['id'] for e in entries
                      if e.get('displayName') == 'smoke-rerank-gpu')
        print('rerank upsert', r2.status_code, second.get('ok'),
              '| entries=', len(entries))
        r = c.post('/api/admin/model-library/activate',
                   json={'section': 'rerank', 'id': gpu_id}, headers=h)
        print('rerank activate', r.status_code, r.json().get('ok'))
        r = c.post('/api/admin/model-library/delete',
                   json={'section': 'rerank', 'id': cpu_id}, headers=h)
        print('rerank delete', r.status_code, r.json().get('ok'),
              '| left=', len((r.json().get('library') or {}).get('entries') or []))

        # YAML 写回验证
        text = CFG.read_text(encoding='utf-8')
        print('yaml has model?', 'test-model' in text,
              '| rerank_model?', 'rerank_model: bge-reranker-v2-m3' in text,
              '| rerank_model_dir?', 'rerank_model_dir: models' in text,
              '| rerank_device=cuda?', 'rerank_device: cuda' in text,
              '| rerank library?', 'smoke-rerank-gpu' in text,
              '| no path in id?', 'models/bge-reranker' not in text,
              '| port int?', 'port: 3306' in text)
        assert first.get('ok'), '第一个重排配置应入库成功'

        # ── VLM 视觉模型：与 LLM 同构的第二块模型库 ──
        # 配置页里这两段**共用一份表单**（点哪块库的卡片就编哪一段），所以这里要
        # 验的正是"没有串段"：VLM 的条目进 vlm 段、llm 段那条原封不动
        r = c.post('/api/admin/model-library/upsert', json={
            'section': 'vlm', 'endpoint': 'http://127.0.0.1:9/v1',
            'modelIds': ['qwen2.5-vl-7b'],
            'configParams': [
                {'key': 'display_name', 'value': 'smoke-vlm', 'type': 'str'},
                {'key': 'model', 'value': 'qwen2.5-vl-7b', 'type': 'str'},
                {'key': 'api_key', 'value': 'sk-vlm', 'type': 'str'},
            ], 'verified': True, 'activate': True}, headers=h)
        vbody = r.json()
        ventries = (vbody.get('library') or {}).get('entries') or []
        print('vlm upsert', r.status_code, vbody.get('ok'),
              '| entries=', len(ventries))
        assert vbody.get('ok') and ventries, 'VLM 条目应入库成功'
        vtext = CFG.read_text(encoding='utf-8')
        # vlm 与 llm 同为顶层段：镜像字段写在段内（vlm.model 等），不是 llm_model 那种
        print('yaml has vlm section?', 'vlm:' in vtext,
              '| vlm model?', 'qwen2.5-vl-7b' in vtext,
              '| vlm library?', 'smoke-vlm' in vtext,
              '| llm untouched?', 'test-model' in vtext)
        assert 'vlm:' in vtext and 'qwen2.5-vl-7b' in vtext, 'vlm 段应落盘'
        # 配置页载荷里要有这段（前端就是按 data.groups 里的 key='vlm' 出「模型库 ·
        # VLM」那一块的；VLM 不单独出卡片，由 config-ui.js 挂在 LLM 卡片里）
        page = c.get('/config', headers=h).text
        has_vlm = bool(re.search(r'"key":\s*"vlm"', page))
        print('config page has vlm group?', has_vlm,
              '| has llm group?', bool(re.search(r'"key":\s*"llm"', page)))
        assert has_vlm, '配置页载荷里应有 VLM 段'
        # 「测试模型」对 vlm 与 llm 同一条路（对话接口）：不可达地址必须如实判失败
        r = c.post('/api/admin/health/test',
                   json={'kind': 'vlm', 'endpoint': 'http://127.0.0.1:9/v1',
                         'model': 'qwen2.5-vl-7b'}, headers=h)
        print('health/test vlm', r.status_code, r.json().get('online'),
              (r.json().get('message') or '')[:50])
        assert r.json().get('online') is False, '不可达的 VLM 地址必须判失败'
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

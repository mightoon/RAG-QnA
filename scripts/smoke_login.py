"""登录流程验证：
1. 浏览器导航（Accept: text/html）无 token → 302 /login
2. API 调用（Accept: application/json）无 token → 401 JSON（不重定向）
3. 登录签发 token → Cookie 访问页面 → 200
4. Authorization 头访问 → 200（原有行为不变）
5. 无效 Cookie → 302 /login
"""
from fastapi.testclient import TestClient
from rag.api.app import create_app_from_env

app = create_app_from_env()
HTML = {"Accept": "text/html,application/xhtml+xml"}
JSON = {"Accept": "application/json"}

with TestClient(app) as c:
    # 1. 浏览器导航无 token → 302
    r = c.get("/", headers=HTML, follow_redirects=False)
    print("1. / no-token(html):", r.status_code,
          "→", r.headers.get("location", ""))
    r = c.get("/chat", headers=HTML, follow_redirects=False)
    print("   /chat no-token(html):", r.status_code,
          "→", r.headers.get("location", ""))

    # 2. API 无 token → 401 JSON
    r = c.get("/api/documents", headers=JSON)
    print("2. /api/documents no-token(json):", r.status_code,
          r.headers.get("content-type", "")[:16])

    # 3. 登录 → Cookie 访问
    t = c.post("/api/auth/token",
               json={"user_id": "u01", "roles": ["admin"]}).json()
    tok = t.get("access_token") or t.get("token")
    print("3. login ok, token len =", len(tok or ""))
    for path in ["/", "/chat", "/knowledge", "/config"]:
        r = c.get(path, headers=HTML, cookies={"rag_token": tok},
                  follow_redirects=False)
        final = r.headers.get("location", "") if r.status_code in (301, 302) else ""
        print(f"   {path} cookie:", r.status_code, final)

    # 4. Authorization 头（原有程序化访问）
    r = c.get("/chat", headers={**HTML, "Authorization": "Bearer " + tok})
    print("4. /chat bearer-header:", r.status_code)

    # 5. 无效 Cookie → 302
    r = c.get("/chat", headers=HTML, cookies={"rag_token": "bad-token"},
              follow_redirects=False)
    print("5. /chat bad-cookie:", r.status_code,
          "→", r.headers.get("location", ""))

    # 6. 登录页本身无需认证
    r = c.get("/login", headers=HTML)
    print("6. /login no-token:", r.status_code)

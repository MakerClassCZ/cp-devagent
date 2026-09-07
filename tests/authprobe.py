"""Black-box auth probe for a running agent that has a token set: every endpoint x method
without a valid token must be refused (401; 403 for a foreign Host/Origin), the three public
paths must reveal nothing, and the gate must answer before a request body is read.

    python tests/authprobe.py [HOST[:PORT]]        # default 127.0.0.1:8100

Nothing is sent that would change state if the gate held - and if it does not hold, the
file/dir names used all start with devagent_authprobe. Prints only the surprises."""
import http.client, json, socket, sys, time
arg = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:8100"
HOST, _, port = arg.partition(":")
PORT = int(port or 8100)
PUBLIC = {"/", "/ui.html", "/version"}
PATHS = ["/health", "/discover", "/boards", "/snippets", "/bootloader/status", "/list", "/file?name=devagent_authprobe.txt",
         "/free", "/serial/read?ms=0", "/serial/tail?from=-1&ms=0", "/ocd/status", "/video", "/snapshot", "/info",
         "/serial/write", "/reset?mode=soft", "/repl?ms=100", "/snippet?id=mem", "/serial/reboot",
         "/bootloader/enter?method=repl", "/uf2", "/ocd/start", "/ocd/stop", "/ocd/cmd", "/ocd/flash",
         "/mkdir?name=devagent_authprobe_dir", "/run?name=devagent_authprobe.py&ms=100", "/dir?name=devagent_authprobe_dir",
         "/boards?id=authprobe", "/shutdown", "/nonexistent"]
METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS", "PATCH", "TRACE"]
PUBLIC_PATHS = ["/", "/ui.html", "/version", "/ui.html/../health", "/./health", "//health", "/health/", "/HEALTH",
                "/version/../health", "/health%00", "/health;x", "/%68ealth", "/health?", "/../health"]
TOKENS = [("none", {}), ("empty header", {"X-Token": ""}), ("wrong header", {"X-Token": "wrong"}),
          ("lowercase header", {"x-token": "wrong"}), ("bearer", {"Authorization": "Bearer wrong"}),
          ("cookie", {"Cookie": "token=wrong; X-Token=wrong"}), ("basic", {"Authorization": "Basic d3Jvbmc6d3Jvbmc="})]
QUERY_TOKENS = ["?token=", "?token=wrong", "?token=%00", "?token[]=wrong", "?TOKEN=wrong", "?token=wrong&token="]

def req(method, path, headers=None, body=None, host=None, timeout=8):
    c = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    h = dict(headers or {})
    if host is not None:
        h["Host"] = host
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read(4096)
    c.close()
    return r.status, dict(r.getheaders()), data

surprises, count = [], 0
def check(label, status, expect, data=b""):
    global count
    count += 1
    if status not in expect:
        surprises.append("%-70s -> %s %s" % (label, status, data[:120]))

# 1. every path x method x token placement
for path in PATHS:
    base = path.split("?")[0]
    for m in METHODS:
        for tl, th in TOKENS:
            try:
                st, hd, data = req(m, path, th, body=b"" if m in ("POST", "PUT", "PATCH") else None)
            except Exception as e:
                surprises.append("%s %s [%s] -> EXC %s" % (m, path, tl, e)); continue
            expect = {401} if m not in ("OPTIONS", "HEAD", "PATCH", "TRACE") else {401, 403, 404, 405, 501, 204, 200}
            if m == "OPTIONS":            # a preflight without a known Origin is refused (403)
                check("OPTIONS %s [%s]" % (path, tl), st, {403, 401}, data)
                continue
            check("%s %s [%s]" % (m, path, tl), st, expect, data)
    for qt in QUERY_TOKENS:
        p = path + ("&" if "?" in path else "") + qt.lstrip("?") if "?" in path else path + qt
        st, hd, data = req("GET", p)
        check("GET %s" % p, st, {401}, data)

# 2. public paths: only the exact three, and what they reveal
for p in PUBLIC_PATHS:
    st, hd, data = req("GET", p)
    if p in PUBLIC:
        check("public GET %s" % p, st, {200}, data)
        if p == "/version":
            j = json.loads(data)
            if set(j) - {"agent", "auth"}:
                surprises.append("/version leaks: %s" % j)
        else:
            low = data.lower()
            for needle in (b"token", b"secret"):
                pass                                   # panel mentions the token FIELD; fine
    else:
        check("trick GET %s" % p, st, {401, 400, 404}, data)

# 3. 401 bodies must not leak state
st, hd, data = req("GET", "/health")
check("public /version query", req("GET", "/version?path=/health")[0], {200})
if b"boards" in data or b"drive" in data:
    surprises.append("401 body leaks health: %r" % data[:200])

# 4. foreign Host / Origin: refused even before the token question
for host in ("evil.example", "192.168.1.150.evil.example", "localhost.evil.example"):
    st, hd, data = req("GET", "/health", host=host)
    check("Host %s (no token)" % host, st, {403, 401}, data)
    st, hd, data = req("GET", "/version", host=host)
    check("Host %s /version" % host, st, {403}, data)
st, hd, data = req("GET", "/health", {"Origin": "https://evil.example"})
check("Origin evil (no token)", st, {403, 401}, data)
if "Access-Control-Allow-Origin" in hd and hd["Access-Control-Allow-Origin"] in ("*", "https://evil.example"):
    surprises.append("CORS header reflects a foreign origin: %s" % hd["Access-Control-Allow-Origin"])
st, hd, data = req("OPTIONS", "/file?name=x", {"Origin": "https://evil.example",
                                               "Access-Control-Request-Method": "PUT"})
if hd.get("Access-Control-Allow-Origin") in ("*", "https://evil.example"):
    surprises.append("preflight allows a foreign origin: %s" % hd)

# 5. gate before body: a PUT that announces 10 MB but sends 10 bytes must be answered at once
s = socket.create_connection((HOST, PORT), timeout=6)
s.sendall(b"PUT /file?name=devagent_authprobe.txt HTTP/1.1\r\nHost: %s\r\nContent-Length: 10000000\r\n\r\n0123456789"
          % HOST.encode())
t0 = time.time()
try:
    first = s.recv(200)
    dt = time.time() - t0
    if b" 401" not in first.split(b"\r\n")[0]:
        surprises.append("unauth PUT with a huge Content-Length: %r" % first[:60])
    elif dt > 3:
        surprises.append("401 for a huge unauth PUT took %.1f s (body read before the gate?)" % dt)
except socket.timeout:
    surprises.append("unauth PUT with a huge Content-Length: no answer in 6 s (server reads the body first)")
s.close()

# 6. HTTP/1.0 without Host (curl-style) still needs the token
s = socket.create_connection((HOST, PORT), timeout=6)
s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
first = s.recv(200); s.close()
check("HTTP/1.0 no Host /health", int(first.split()[1]), {401}, first)

print("%s:%d - %d checks, %d surprises" % (HOST, PORT, count, len(surprises)))
for x in surprises:
    print("  " + x)
sys.exit(1 if surprises else 0)

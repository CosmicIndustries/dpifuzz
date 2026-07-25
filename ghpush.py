#!/usr/bin/env python3
"""Minimal GitHub file pusher — stdlib only, reads the PAT from a file so it
never appears in a command line, shell history, or chat transcript.

  echo 'ghp_xxx' > ~/.gh_token && chmod 600 ~/.gh_token
  ./ghpush.py OWNER/REPO "commit message" local_path:remote_path [...]
"""
import base64, json, os, sys, urllib.request, urllib.error
from pathlib import Path

tok = Path(os.path.expanduser("~/.gh_token")).read_text().strip()
if not tok:
    sys.exit("~/.gh_token is empty")
repo, msg, *pairs = sys.argv[1:]
if not pairs:
    sys.exit("no files given")

NEW = "--new" in pairs
DESC = ""
if NEW:
    pairs = [p for p in pairs if p != "--new"]
    for i, p in enumerate(pairs):
        if p.startswith("--desc="):
            DESC = p[7:]; pairs.pop(i); break


def api(method, url, body=None):
    r = urllib.request.Request(
        f"https://api.github.com{url}", method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "ghpush", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as f:
            return f.status, json.load(f)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


st, who = api("GET", "/user")
if st != 200:
    sys.exit(f"auth failed ({st}: {who.get('message', who)}).\n"
             f"~/.gh_token holds {len(tok)} chars starting {tok[:4]!r} — if that "
             f"looks like a placeholder rather than a real token, that is the problem.\n"
             f"New token: https://github.com/settings/tokens/new  (scope: public_repo)")
print(f"authenticated as {who.get('login')}")

if NEW:
    owner, _, name = repo.partition("/")
    st, res = api("GET", f"/repos/{repo}")
    if st == 200:
        print(f"{repo} already exists — updating in place")
    elif st == 404:
        st, res = api("POST", "/user/repos",
                      {"name": name, "description": DESC, "private": False,
                       "auto_init": False})
        print(f"created {repo}" if st == 201
              else f"create failed: {st} {res.get('message', res)}")
    else:
        sys.exit(f"cannot reach {repo}: {st} {res.get('message', res)}")

for pair in pairs:
    local, _, remote = pair.partition(":")
    remote = remote or Path(local).name
    data = Path(local).read_bytes()
    st, cur = api("GET", f"/repos/{repo}/contents/{remote}")
    body = {"message": msg,
            "content": base64.b64encode(data).decode()}
    if st == 200 and isinstance(cur, dict) and "sha" in cur:
        body["sha"] = cur["sha"]          # update in place
    st, res = api("PUT", f"/repos/{repo}/contents/{remote}", body)
    if st in (200, 201):
        print(f"  ok  {remote:<28} {len(data):>7} B  "
              f"{res.get('commit', {}).get('sha', '')[:7]}")
    else:
        print(f"  FAIL {remote}: {st} {res.get('message', res)}")

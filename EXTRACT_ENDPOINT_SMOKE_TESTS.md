# `/api/extract/render-text` — Post-Deploy Smoke Tests

Six manual checks for the Wolfee URL-extraction endpoint. Run after each
deploy. Replace `$BASE` with the live URL and `$KEY` with `EXTRACT_API_KEY`.

```bash
BASE=https://axiom-browser-agent-clone-production.up.railway.app
KEY=<the value of EXTRACT_API_KEY>
```

---

## 1. 401 — missing key

```bash
curl -i -s -X POST "$BASE/api/extract/render-text" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.linkedin.com/jobs/view/4406118990"}'
```

**Expected:** `HTTP/2 401` with body
```json
{"error":"Invalid or missing API key"}
```

---

## 2. 401 — wrong key

```bash
curl -i -s -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: not-the-real-key" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.linkedin.com/jobs/view/4406118990"}'
```

**Expected:** `HTTP/2 401` with the same body as #1.

---

## 3. 200 — real LinkedIn URL (the happy path)

Use a recently-posted live job ID from
`https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?keywords=software%20engineer&start=0`
(grep for `urn:li:jobPosting:<id>`). Anything older than ~30 days is more
likely to have hit `expired_redirect`.

```bash
curl -s -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url":"https://www.linkedin.com/jobs/view/4406118990",
    "wait_for_selector":"div.description__text"
  }' | jq '.ok, .title, (.text | length), (.jd_text | length), .duration_ms'
```

**Expected:** `ok: true`, a non-empty `title` matching
`"<Company> hiring <Role> in <Location> | LinkedIn"`, a `text` length in
the few thousand chars, and a `jd_text` length under that (the JD subset).
Cold call: 6–10s; warm: 4–7s.

Sample (truncated):
```json
{
  "ok": true,
  "url": "https://www.linkedin.com/jobs/view/4406118990",
  "final_url": "https://www.linkedin.com/jobs/view/4406118990",
  "status": 200,
  "title": "Notion hiring Software Engineer, New Grad in San Francisco, CA | LinkedIn",
  "text": "Skip to main content | LinkedIn | Jobs | ...",
  "jd_text": "About Us | Notion is on a mission to ...",
  "duration_ms": 4682
}
```

---

## 4. 200 with `ok:false reason:"expired_redirect"`

LinkedIn silently bounces invalid IDs to a search page that *also* fills
the JD selector with a different job's data. The endpoint catches this
by comparing the job-ID before and after navigation.

```bash
curl -s -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.linkedin.com/jobs/view/1111111111"}' | jq
```

**Expected:**
```json
{
  "ok": false,
  "url": "https://www.linkedin.com/jobs/view/1111111111",
  "reason": "expired_redirect",
  "final_url": "https://www.linkedin.com/jobs/<role>-jobs?...expired_jd_redirect...",
  "status": 200,
  "title": null,
  "duration_ms": <ms>
}
```

---

## 5. 200 with `ok:false reason:"blocked"` — Indeed (expected to fail)

Per the audit, Indeed Cloudflare-403s any datacenter IP. The endpoint
should detect that and return a controlled failure rather than 5xx-ing.

```bash
curl -s -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.indeed.com/viewjob?jk=11111111aaaa1111"}' | jq
```

**Expected:** `ok: false, reason: "blocked"` (Cloudflare "Just a moment..." page detected).
If Indeed ever stops blocking us, this test will start returning `ok: true` — that's
a signal worth noticing, not a failure.

---

## 6. 429 — concurrency lock

Fire two requests in parallel; the singleton runtime serves one and the
second should fast-fail with 429 within ~200ms.

```bash
bash -c "
  curl -s -o /tmp/r1.json -w 'r1: %{http_code}\n' -X POST '$BASE/api/extract/render-text' \
    -H 'X-API-Key: $KEY' -H 'Content-Type: application/json' \
    -d '{\"url\":\"https://www.linkedin.com/jobs/view/4406118990\"}' &
  curl -s -o /tmp/r2.json -w 'r2: %{http_code}\n' -X POST '$BASE/api/extract/render-text' \
    -H 'X-API-Key: $KEY' -H 'Content-Type: application/json' \
    -d '{\"url\":\"https://www.linkedin.com/jobs/view/4406118990\"}' &
  wait
"
cat /tmp/r1.json /tmp/r2.json
```

**Expected:** one of `r1` / `r2` is `200`, the other is `429` with body:
```json
{"error":"Service busy, retry in a moment"}
```

If both come back `200` (very fast machines, or a render that finishes inside
the 200ms acquire window), repeat — the test is racy by nature. The
acceptance check is: a 429 is reproducible within a few attempts.

---

## Validation failures (bonus)

These return 400, not the `{ok:false,reason}` shape, because the request itself
is malformed:

```bash
# Non-http(s) scheme
curl -s -i -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"file:///etc/passwd"}'                 # 400

# Embedded credentials
curl -s -i -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"https://user:pass@example.com/"}'    # 400

# Private host
curl -s -i -X POST "$BASE/api/extract/render-text" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"url":"http://localhost/"}'                  # 400
```

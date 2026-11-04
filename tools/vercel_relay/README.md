# Mumbai relay (free, Vercel) — exam links from an Indian IP

Exam portals open only from Indian IP addresses; the bot runs abroad (GitHub Actions) and gets `403`.
This tiny Vercel function runs in **Mumbai (`bom1`)**: the bot asks it to open each exam link, it returns the
page, and the bot does all the work (marks, scorecard, PDF) itself. Free plan, no card, no PC.

```
user → bot (GitHub, USA) ──"open this link"──▶ Vercel function (Mumbai) ──▶ exam site ✅
                         ◀──── the page ─────
bot makes the scorecard / PDF → user
```

Caveat: the Mumbai IP is a data-centre (AWS) address. Sites that block only foreign IPs accept it; a site that
also blocks data centres would not — **step 3 checks this before anything else is built on it.**

## 1. Deploy (browser only, ≈ 5 minutes)
1. <https://vercel.com/signup> → **Continue with GitHub** (Hobby plan, free).
2. **Add New… → Project** → import **`rvsh-bot`** (allow Vercel access to the repository when asked).
3. Before "Deploy":
   - **Root Directory** → Edit → `tools/vercel_relay`
   - Framework Preset → **Other**
   - **Environment Variables** → Name `RELAY_TOKEN`, Value: a long secret (e.g. 30 random letters/digits)
4. **Deploy**. The project address looks like `https://rvsh-relay-xxxx.vercel.app`.

## 2. Is it in Mumbai?
Open in the browser:
```
https://<your-project>.vercel.app/api/relay?token=<RELAY_TOKEN>&ip=1
```
→ should show `"country": "IN"` (city Mumbai). If not: Project → Settings → Functions → Function Region →
**Mumbai, India (bom1)** → Redeploy.

## 3. Does the exam site open? (the real test)
```
https://<your-project>.vercel.app/api/relay?token=<RELAY_TOKEN>&url=<an exam response-sheet link>
```
- The page (answer sheet) appears, or the page source starts with `<!-- relay: site answered 200` → ✅ works.
- `site answered 403` → the site blocks this IP too; use an Indian residential proxy instead (`HTTP_PROXY`).

## 4. The bot
GitHub → repository → Settings → Secrets and variables → Actions → **New repository secret**:
| Name | Value |
|---|---|
| `FETCH_RELAY_URL` | `https://<your-project>.vercel.app` |
| `FETCH_RELAY_TOKEN` | the same value as `RELAY_TOKEN` on Vercel |

Then start a new run (Actions → bot → Run workflow). Elsewhere (Docker / VPS): the same two lines in `.env`, or
`/bot` → 🔑 config.env → Mumbai relay URL / token (used at once, no restart).

`/bot` → Network → Fetching links: **Direct** (every link through the relay) or **Auto** (relay; the fetch server
only if the relay fails and one is set). **Remote** skips the relay.

How the bot uses it: pages and the other-language postbacks one request per call (cookies and redirects kept by
the bot); a sheet's images ~40 per call — about 15 calls per sheet.

## Limits (Vercel Hobby)
60 s per request, 4.5 MB per answer (one page or one image — fine for exam sheets), a monthly free allowance of
function time and traffic, and personal / non-commercial use under Vercel's terms.

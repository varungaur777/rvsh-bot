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
Bot support for the relay is the next step (after step 3 says ✅): settings `FETCH_RELAY_URL` +
`FETCH_RELAY_TOKEN`.

## Limits (Vercel Hobby)
60 s per request, 4.5 MB per answer (one page or one image — fine for exam sheets), a monthly free allowance of
function time and traffic, and personal / non-commercial use under Vercel's terms.

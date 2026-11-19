# Competitor Newsroom

One page that pulls news from every tracked company's newsroom, updated every 15 minutes.

- **News** tab: one feed with filters for program, list, priority, company, date and free-text search. NEW badges, unread-only view, mark all read, CSV export.
- **Companies** tab: the tracked company list with assets and keyword filters.
- **Sources** tab: how each company's news is collected and whether its feed is working.

## How it works

```
companies.yaml  ──►  scripts/fetch.py (GitHub Action, every 15 min)  ──►  data/news.json  ──►  index.html (GitHub Pages)
```

For each company the fetcher uses the best source it can find:

1. A feed URL set by hand in `companies.yaml` (`rss:`)
2. The newsroom's own RSS/Atom feed, found on the newsroom page or the homepage
3. Common feed locations (`/feed`, `/rss`, and the usual Q4 and GCS investor-site feeds)
4. Google News RSS restricted to the company's own website (`site:company.com`), for newsrooms without RSS
5. A Google News search on the company name, only for companies with no website listed

Large, diversified companies have a `keywords:` list, so only items that mention the tracked assets or indications get through.

## One-time setup

1. Create a **new** public repository (for example `competitor-newsroom`) and upload everything in this folder, keeping the folder structure. Check that `.github/workflows/update.yml` uploaded. The web uploader sometimes skips folders that start with a dot.
2. **Settings → Actions → General → Workflow permissions:** select **Read and write permissions**, then click Save.
3. **Settings → Pages → Source:** select **GitHub Actions**.
4. **Actions** tab: enable workflows if asked. Then **Update competitor news → Run workflow**. The first run takes about 3–5 minutes and backfills recent news, which isn't marked NEW.
5. The site address is shown in **Settings → Pages**.

The page has a `noindex` tag, so search engines are asked not to list it. Anyone who has the link can still open it.

## Day-to-day

| Task | How |
|---|---|
| Add or remove a company | Edit `companies.yaml` in GitHub and commit. The site updates within a few minutes. |
| A feed is wrong or missing | Check the **Sources** tab. Put the correct feed URL in `rss:` for that company. |
| Too much noise from a big company | Add or tighten its `keywords:` list. |
| Force a refresh now | Actions → Update competitor news → Run workflow |

## Notes

- **Speed:** GitHub runs scheduled jobs every 15 minutes, but can delay them by a few minutes when it's busy. The page checks for new data every 5 minutes. Google News feeds usually lag the newsroom by 15 to 60 minutes.
- **Read/unread** status is kept in each person's own browser. It isn't shared across the team.
- History is kept for 12 months in `data/news.json`.

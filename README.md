# GitHub Student Analytics Platform

This Streamlit app analyzes GitHub student data from an uploaded Excel sheet and provides analytics dashboards with profile summaries, repository metrics, and leaderboard insights.

## Deploy on Streamlit Community Cloud

1. Push this project to a GitHub repository.
2. Open Streamlit Community Cloud.
3. Click New app and select the repository, branch, and main file `app.py`.
4. Add the required secret `GITHUB_TOKEN` if you want higher GitHub API rate limits.

## Local run

```bash
cd Github-website-
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

# CleanCloud Article Export

Render-ready web app for exporting CleanCloud Cleaning/Ready article data to Excel.

## Local Run

```powershell
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8000`, enter CleanCloud credentials, choose tabs, and download the generated Excel file when the job completes.

## Render

This repo includes `Dockerfile` and `render.yaml`. Create a new Render web service from the GitHub repository and Render will build the Docker image with Chromium for Selenium.

Credentials are entered per export in the UI. They are passed to the scraper process through environment variables and are not saved to files.

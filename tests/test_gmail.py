from app.gmail import get_job_alert_emails

emails = get_job_alert_emails(max_results=3)
for e in emails:
    print(f"Subject : {e['subject']}")
    print(f"Date    : {e['date']}")
    print(f"URL     : {e['see_all_jobs_url']}")
    print()

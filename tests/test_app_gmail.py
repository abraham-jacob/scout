"""Tests for app/gmail.py — Gmail integration."""

import pytest
import base64
import json
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path

from app.gmail import (
    extract_see_all_jobs_url,
    _extract_body,
    get_label_id,
    get_gmail_service,
    get_job_alert_emails,
    mark_as_read,
)


class TestExtractSeeAllJobsUrl:
    """Test extracting 'See all jobs' URL from email body."""

    def test_extract_linkedin_see_all_jobs_link(self):
        """Extract 'See all jobs' link from email HTML."""
        body = """
        <html>
            <a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=engineer&distance=25">
                See all jobs
            </a>
        </html>
        """

        url = extract_see_all_jobs_url(body)

        assert url is not None
        assert "linkedin.com/comm/jobs/search-results" in url

    def test_extract_linkedin_tracking_url(self):
        """Extract LinkedIn tracking URL with all parameters."""
        body = """
        <a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=python&geoId=90000084&trk=email_alert">
            See all jobs
        </a>
        """

        url = extract_see_all_jobs_url(body)

        assert url is not None
        assert "keywords=python" in url
        assert "linkedin.com" in url

    def test_extract_case_insensitive(self):
        """URL extraction is case-insensitive."""
        body = """
        <a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=engineer">
            SEE ALL JOBS
        </a>
        """

        url = extract_see_all_jobs_url(body)

        assert url is not None
        assert "linkedin.com" in url

    def test_fallback_to_any_linkedin_jobs_url(self):
        """Fallback to any LinkedIn jobs search URL if no "See all jobs" found."""
        body = """
        <html>
            Some content
            https://www.linkedin.com/comm/jobs/search?keywords=manager&distance=50
            More content
        </html>
        """

        url = extract_see_all_jobs_url(body)

        assert url is not None
        assert "linkedin.com" in url

    def test_no_url_found(self):
        """Return None if no LinkedIn URL found."""
        body = """
        <html>
            No LinkedIn content here
        </html>
        """

        url = extract_see_all_jobs_url(body)

        assert url is None

    def test_empty_body(self):
        """Return None for empty body."""
        url = extract_see_all_jobs_url("")

        assert url is None

    def test_complex_html_with_multiple_links(self):
        """Extract correct link from complex HTML with multiple links."""
        body = """
        <html>
            <a href="https://example.com">Other link</a>
            <a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=data&distance=25">
                See all jobs
            </a>
            <a href="https://twitter.com">Twitter</a>
        </html>
        """

        url = extract_see_all_jobs_url(body)

        assert url is not None
        assert "linkedin.com/comm/jobs" in url


class TestExtractBody:
    """Test extracting text body from Gmail payload."""

    def test_extract_simple_payload(self):
        """Extract text from simple payload."""
        body_text = "This is the email body"
        encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
        payload = {
            "body": {"data": encoded}
        }

        result = _extract_body(payload)

        assert result == body_text

    def test_extract_nested_multipart(self):
        """Extract text from nested multipart payload."""
        body_text = "Text content"
        encoded = base64.urlsafe_b64encode(body_text.encode()).decode()
        payload = {
            "parts": [
                {"body": {}},  # Empty part
                {
                    "parts": [
                        {"body": {"data": encoded}},  # Nested text
                    ]
                },
            ]
        }

        result = _extract_body(payload)

        assert body_text in result

    def test_extract_empty_payload(self):
        """Return empty string for empty payload."""
        result = _extract_body({})

        assert result == ""

    def test_extract_invalid_base64(self):
        """Handle invalid base64 data gracefully."""
        payload = {
            "body": {"data": "not-valid-base64!!!"}
        }

        result = _extract_body(payload)

        # Should not raise, returns decoded or empty
        assert isinstance(result, str)

    def test_extract_html_content(self):
        """Extract HTML content correctly."""
        html_content = "<html><body>Test email</body></html>"
        encoded = base64.urlsafe_b64encode(html_content.encode()).decode()
        payload = {
            "body": {"data": encoded}
        }

        result = _extract_body(payload)

        assert "Test email" in result

    def test_extract_utf8_content(self):
        """Extract UTF-8 encoded content."""
        content = "Email with émojis 📧"
        encoded = base64.urlsafe_b64encode(content.encode('utf-8')).decode()
        payload = {
            "body": {"data": encoded}
        }

        result = _extract_body(payload)

        assert "émojis" in result


class TestGetLabelId:
    """Test getting Gmail label ID."""

    def test_get_label_id_found(self):
        """Get label ID when label exists."""
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "label1", "name": "Work"},
                {"id": "label2", "name": "Job Alerts"},
            ]
        }

        label_id = get_label_id(mock_service, "Job Alerts")

        assert label_id == "label2"

    def test_get_label_id_case_insensitive(self):
        """Label lookup is case-insensitive."""
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "label1", "name": "Job Alerts"},
            ]
        }

        label_id = get_label_id(mock_service, "job alerts")

        assert label_id == "label1"

    def test_get_label_id_not_found(self):
        """Return None if label doesn't exist."""
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {
            "labels": [
                {"id": "label1", "name": "Work"},
            ]
        }

        label_id = get_label_id(mock_service, "Nonexistent Label")

        assert label_id is None

    def test_get_label_id_empty_labels(self):
        """Handle empty labels list."""
        mock_service = MagicMock()
        mock_service.users().labels().list().execute.return_value = {
            "labels": []
        }

        label_id = get_label_id(mock_service, "Any Label")

        assert label_id is None


class TestGetGmailService:
    """Test Gmail service authentication."""

    @patch('app.gmail.TOKEN_FILE')
    @patch('app.gmail.Credentials')
    @patch('app.gmail.build')
    def test_get_gmail_service_with_cached_token(self, mock_build, mock_creds_class, mock_token_file):
        """Get service when token is cached."""
        mock_token_file.exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds_class.from_authorized_user_file.return_value = mock_creds
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        service = get_gmail_service()

        assert service == mock_service
        mock_creds_class.from_authorized_user_file.assert_called_once()

    @patch('app.gmail.TOKEN_FILE')
    @patch('app.gmail.CREDENTIALS_FILE')
    @patch('app.gmail.Credentials')
    @patch('app.gmail.InstalledAppFlow')
    @patch('app.gmail.build')
    def test_get_gmail_service_refresh_expired_token(self, mock_build, mock_flow_class,
                                                     mock_creds_class, mock_creds_file, mock_token_file):
        """Refresh token if expired but refresh_token available."""
        mock_token_file.exists.return_value = True
        mock_creds = MagicMock()
        mock_creds.valid = False
        mock_creds.expired = True
        mock_creds.refresh_token = "refresh_token"
        mock_creds_class.from_authorized_user_file.return_value = mock_creds
        mock_service = MagicMock()
        mock_build.return_value = mock_service

        service = get_gmail_service()

        assert service == mock_service
        mock_creds.refresh.assert_called_once()


class TestMarkAsRead:
    """Test marking emails as read."""

    def test_mark_as_read(self):
        """Mark message as read by removing UNREAD label."""
        mock_service = MagicMock()

        mark_as_read(mock_service, "msg123")

        mock_service.users().messages().modify.assert_called_once()
        call_args = mock_service.users().messages().modify.call_args
        assert call_args[1]["id"] == "msg123"
        assert "removeLabelIds" in call_args[1]["body"]
        assert "UNREAD" in call_args[1]["body"]["removeLabelIds"]


class TestGetJobAlertEmails:
    """Test fetching job alert emails from Gmail."""

    @patch('app.gmail.get_gmail_service')
    @patch('app.gmail.get_label_id')
    @patch('app.gmail.mark_as_read')
    def test_get_job_alert_emails(self, mock_mark_read, mock_get_label, mock_get_service):
        """Fetch job alert emails successfully."""
        # Setup mocks
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_get_label.return_value = "label_id"

        # Mock the messages list call
        mock_service.users().messages().list().execute.return_value = {
            "messages": [{"id": "msg1"}]
        }

        # Mock the message get call
        mock_message_data = {
            "id": "msg1",
            "payload": {
                "headers": [
                    {"name": "Subject", "value": "LinkedIn Job Alert"},
                    {"name": "Date", "value": "Thu, 01 Jan 2026 12:00:00 +0000"},
                ],
                "body": {
                    "data": base64.urlsafe_b64encode(
                        b'<a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=python">See all jobs</a>'
                    ).decode()
                }
            }
        }
        mock_service.users().messages().get().execute.return_value = mock_message_data

        emails = get_job_alert_emails(max_results=1)

        assert len(emails) == 1
        assert emails[0]["subject"] == "LinkedIn Job Alert"
        assert emails[0]["message_id"] == "msg1"
        assert "linkedin.com" in emails[0]["see_all_jobs_url"]
        # Emails are no longer marked read during extraction — the runner does
        # that per-email only after the jobs are written to the database.
        mock_mark_read.assert_not_called()

    @patch('app.gmail.get_gmail_service')
    @patch('app.gmail.get_label_id')
    def test_get_job_alert_emails_label_not_found(self, mock_get_label, mock_get_service):
        """Raise error if label not found."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_get_label.return_value = None

        with pytest.raises(ValueError, match="Gmail label"):
            get_job_alert_emails()

    @patch('app.gmail.get_gmail_service')
    @patch('app.gmail.get_label_id')
    def test_get_job_alert_emails_empty(self, mock_get_label, mock_get_service):
        """Handle empty email list."""
        mock_service = MagicMock()
        mock_get_service.return_value = mock_service
        mock_get_label.return_value = "label_id"
        mock_service.users().messages().list().execute.return_value = {
            "messages": []
        }

        emails = get_job_alert_emails()

        assert emails == []

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services.document_mind_parser_service import DocumentMindParserService


def test_extract_job_id_should_support_body_data_id_uppercase():
    payload = {
        "body": {
            "Data": {
                "Id": "docmind-20260302-c61241381bd64c42a504ad6efdd6fa5a",
            }
        }
    }
    job_id = DocumentMindParserService._extract_job_id(payload)
    assert job_id == "docmind-20260302-c61241381bd64c42a504ad6efdd6fa5a"


def test_extract_job_id_should_support_body_data_id_lowercase():
    payload = {
        "body": {
            "data": {
                "id": "docmind-123",
            }
        }
    }
    job_id = DocumentMindParserService._extract_job_id(payload)
    assert job_id == "docmind-123"


def test_extract_status_should_support_capitalized_fields():
    payload = {
        "body": {
            "Data": {
                "Status": "Success",
            }
        }
    }
    status = DocumentMindParserService._extract_status(payload)
    assert status == "success"


def test_filter_doc_structure_to_page_should_keep_only_target_page_layouts():
    svc = DocumentMindParserService()
    data = {
        "layouts": [
            {"uniqueId": "a", "pageNum": [0], "text": "page1"},
            {"uniqueId": "b", "pageNum": [2], "text": "page2"},
            {"uniqueId": "c", "text": "no_page_tag"},
        ]
    }
    filtered = svc._filter_doc_structure_to_page(data=data, page=1)
    layouts = filtered.get("layouts") or []
    ids = [row.get("uniqueId") for row in layouts]
    assert "a" in ids
    assert "b" not in ids
    assert "c" in ids

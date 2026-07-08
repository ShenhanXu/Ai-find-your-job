import json

from app.mcp_server import handle_jsonrpc


def test_mcp_lists_job_tools():
    response = handle_jsonrpc(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))

    assert response["id"] == 1
    tool_names = {tool["name"] for tool in response["result"]["tools"]}
    assert {"search_jobs", "get_job_details", "prepare_application_action"} <= tool_names
    assert "match_resume" not in tool_names


def test_mcp_search_jobs_returns_structured_content():
    response = handle_jsonrpc(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_jobs",
                    "arguments": {"query": "Java", "audience": "new-grad", "limit": 3},
                },
            }
        )
    )

    result = response["result"]
    assert result["structuredContent"]["items"]
    assert result["content"][0]["type"] == "text"


def test_mcp_get_job_details_returns_full_description():
    search = handle_jsonrpc(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "search_jobs",
                    "arguments": {"query": "Java", "audience": "new-grad", "limit": 1},
                },
            }
        )
    )
    job_id = search["result"]["structuredContent"]["items"][0]["id"]

    response = handle_jsonrpc(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_job_details",
                    "arguments": {"job_ids": [job_id]},
                },
            }
        )
    )

    item = response["result"]["structuredContent"]["items"][0]
    assert item["id"] == job_id
    assert item["description"]

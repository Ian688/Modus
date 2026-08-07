"""Plan-and-Execute planning: pure plan model + JSON parser."""

from __future__ import annotations

from modus.agent.planning import ExecutionPlan, PlanTask, parse_plan


def test_parse_plan_from_json():
    text = '''{"summary": "重构缓存", "tasks": [
      {"id": "t1", "description": "读缓存代码", "type": "file_read"},
      {"id": "t2", "description": "改缓存实现", "type": "file_write", "dependencies": ["t1"]}
    ]}'''
    plan = parse_plan("重构缓存", text)
    assert plan is not None
    assert plan.goal == "重构缓存"
    assert len(plan.tasks) == 2
    assert plan.execution_order == ["t1", "t2"]


def test_parse_plan_tolerates_fences_and_prose():
    text = "Here is my plan:\n```json\n{\"summary\":\"x\",\"tasks\":[{\"id\":\"a\",\"description\":\"do it\"}]}\n```"
    plan = parse_plan("goal", text)
    assert plan is not None
    assert plan.tasks[0].id == "a"


def test_parse_plan_rejects_cycle():
    text = '''{"tasks": [
      {"id": "a", "description": "A", "dependencies": ["b"]},
      {"id": "b", "description": "B", "dependencies": ["a"]}
    ]}'''
    assert parse_plan("cycle", text) is None


def test_parse_plan_rejects_missing_dependency():
    text = '''{"tasks": [{"id": "a", "description": "A", "dependencies": ["ghost"]}]}'''
    assert parse_plan("missing", text) is None


def test_parse_plan_returns_none_on_garbage():
    assert parse_plan("goal", "not json at all") is None
    assert parse_plan("goal", "") is None


def test_ready_tasks_respects_dependencies():
    plan = parse_plan("goal", '''{"tasks": [
      {"id": "a", "description": "A"},
      {"id": "b", "description": "B", "dependencies": ["a"]}
    ]}''')
    assert plan is not None
    ready = plan.ready_tasks(set())
    assert [t.id for t in ready] == ["a"]
    assert [t.id for t in plan.ready_tasks({"a"})] == ["b"]

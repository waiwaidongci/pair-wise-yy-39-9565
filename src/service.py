from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from . import rules
from .domain import (ValidationError, ensure_role, normalize_severity,
                     require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, RECORD_ROLES, TITLE,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def backfill_readings(self, item_id: int, payload: Dict[str, Any], actor: str,
                          role: str) -> Dict[str, Any]:
        """断网后按批次补传现场读数，并入已有缺陷。

        读数的现场观测时间早于处置时间也会保留并参与复核重算；同一批次重复提交
        幂等返回；版本冲突时由仓库抛出携带 current_version 的 ConflictError。
        """
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        self.repository.get_item(item_id)
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValidationError("expected_version必须是正整数")
        raw_readings = payload.get("readings")
        if not isinstance(raw_readings, list) or len(raw_readings) == 0:
            raise ValidationError("readings必须是非空数组")
        batch_id = payload.get("batch_id")
        if batch_id is None or (isinstance(batch_id, str) and not batch_id.strip()):
            batch_id = f"batch-{uuid.uuid4().hex}"
        else:
            batch_id = require_text(batch_id, "batch_id", 100)
        readings = []
        for raw in raw_readings:
            if not isinstance(raw, dict):
                raise ValidationError("每条读数必须是对象")
            kind = require_text(raw.get("kind"), "kind", 50)
            if kind not in rules.READING_KINDS:
                raise ValidationError("kind必须是seepage或displacement")
            value = require_number(raw.get("value"), "value")
            unit = raw.get("unit")
            if unit is not None:
                unit = require_text(unit, "unit", 20)
            observed_at = require_text(raw.get("observed_at"), "observed_at", 40)
            readings.append({
                "kind": kind, "value": value, "unit": unit, "observed_at": observed_at,
            })
        return self.repository.backfill_readings(
            item_id, expected_version, batch_id, readings, actor)

    def recalculate_review(self, item_id: int, actor: str, role: str) -> Dict[str, Any]:
        """读数更新后手动重算复核结论，旧结论失效但历史可查。"""
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        return self.repository.recalculate_review(item_id, actor)

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_readings(item_id)

    def list_reviews(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_reviews(item_id)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result

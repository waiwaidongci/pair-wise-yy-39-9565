from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ValidationError, ensure_role, normalize_reading_kind,
                     normalize_severity, require_number, require_text,
                     require_timestamp)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DEFAULT_READING_UNITS, ENTITY,
                    RECORD_ROLES, SYNC_ROLES, TITLE, VIEW_ROLES,
                    completion_blockers, compute_conclusion,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
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

    @staticmethod
    def _validate_reading(entry: Any) -> Dict[str, Any]:
        if not isinstance(entry, dict):
            raise ValidationError("读数必须是JSON对象")
        kind = normalize_reading_kind(entry.get("kind"))
        value = require_number(entry.get("value"), "value")
        unit = require_text(entry.get("unit") or DEFAULT_READING_UNITS[kind], "unit", 20)
        observed_at = require_timestamp(entry.get("observed_at"), "observed_at")
        external_ref = entry.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        return {"kind": kind, "value": value, "unit": unit,
                "observed_at": observed_at, "external_ref": external_ref}

    def sync_readings(self, item_id: int, payload: Dict[str, Any], actor: str,
                      role: str) -> tuple:
        """离线补传：整批校验后一次性落库；batch_id幂等，版本冲突时返回当前版本。"""
        ensure_role(role, SYNC_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_id = require_text(payload.get("batch_id"), "batch_id", 100)
        expected = payload.get("expected_version")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
            raise ValidationError("expected_version必须是正整数")
        raw = payload.get("readings")
        if not isinstance(raw, list) or not raw:
            raise ValidationError("readings必须是非空数组")
        if len(raw) > 200:
            raise ValidationError("单批次读数不能超过200条")
        readings = [self._validate_reading(entry) for entry in raw]
        result, replayed = self.repository.apply_reading_batch(
            item_id, batch_id, expected, readings, actor,
            lambda item, latest: compute_conclusion(
                item["severity"], item["threshold"], latest))
        result["replayed"] = replayed
        return result, replayed

    def list_readings(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_readings(item_id)

    def list_conclusions(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_conclusions(item_id)

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

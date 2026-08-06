(function (global) {
  "use strict";

  const EVENT_SCHEMA = "modus.agent-event.v2";
  const DESKTOP_PROTOCOL_VERSION = 2;
  const RUN_CONNECTION_ROLES = Object.freeze({
    OWNER: "owner",
    OBSERVER: "observer",
    DETACHED: "detached",
    UNKNOWN: "unknown",
  });

  function stringValue(value) {
    return value === null || value === undefined ? "" : String(value);
  }

  function normalizeAgentEvent(value) {
    if (!value || typeof value !== "object") return null;
    const eventId = stringValue(value.event_id).trim();
    const runId = stringValue(value.run_id).trim();
    const type = stringValue(value.type).trim();
    if (!eventId || !runId || !type) return null;
    const payload = value.payload && typeof value.payload === "object" ? value.payload : {};
    const artifactIds = Array.isArray(value.artifact_ids)
      ? value.artifact_ids.map(stringValue).filter(Boolean)
      : payload.artifact_id ? [stringValue(payload.artifact_id)] : [];
    return {
      ...value,
      schema: stringValue(value.schema || EVENT_SCHEMA),
      event_id: eventId,
      run_id: runId,
      workspace_id: stringValue(value.workspace_id),
      task_id: stringValue(value.task_id || payload.task_id) || null,
      part_id: stringValue(value.part_id || eventId),
      artifact_ids: artifactIds,
      workbench: value.workbench && typeof value.workbench === "object" ? value.workbench : null,
      sequence: Math.max(0, Number(value.sequence || 0)),
      revision: Math.max(0, Number(value.revision || 0)),
      actor: value.actor && typeof value.actor === "object" ? value.actor : {},
      payload,
    };
  }

  function runAdmissionConnectionRole(packet) {
    if (packet?.run_owned_by_connection === true) {
      return RUN_CONNECTION_ROLES.OWNER;
    }
    if (packet?.run_owned_by_connection !== false) {
      return RUN_CONNECTION_ROLES.UNKNOWN;
    }
    // `owned` describes whether any live runtime owns the durable Run.  It is
    // deliberately distinct from this connection's authority to stop it.
    if (packet?.owned === true) return RUN_CONNECTION_ROLES.OBSERVER;
    if (packet?.owned === false) return RUN_CONNECTION_ROLES.DETACHED;
    return RUN_CONNECTION_ROLES.UNKNOWN;
  }

  function runSettlementConnectionRole(packet) {
    if (packet?.run_owned_by_connection === true) {
      return RUN_CONNECTION_ROLES.OWNER;
    }
    if (packet?.run_owned_by_connection === false) {
      return RUN_CONNECTION_ROLES.OBSERVER;
    }
    return RUN_CONNECTION_ROLES.UNKNOWN;
  }

  class ProtocolStateStore {
    constructor() {
      this.runtimeSessionId = "";
      this.sessionId = "";
      this.workspace = null;
    }
    bindIdentity(packet) {
      if (!packet || typeof packet !== "object") return;
      this.runtimeSessionId = stringValue(
        packet.runtime_session_id || packet.session_id || this.runtimeSessionId,
      );
      if (Object.prototype.hasOwnProperty.call(packet, "db_id")) {
        this.sessionId = stringValue(packet.db_id);
      }
      if (packet.workspace && typeof packet.workspace === "object") {
        this.workspace = {...packet.workspace};
      }
    }
    resetConversation(sessionId) {
      this.sessionId = stringValue(sessionId);
    }
  }

  global.ModusProtocol = {
    EVENT_SCHEMA, DESKTOP_PROTOCOL_VERSION,
    RUN_CONNECTION_ROLES,
    normalizeAgentEvent, runAdmissionConnectionRole,
    runSettlementConnectionRole, ProtocolStateStore,
  };
})(window);

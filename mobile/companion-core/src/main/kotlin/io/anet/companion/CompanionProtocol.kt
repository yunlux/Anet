package io.anet.companion

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put

object CompanionProtocol {
    const val PROTOCOL = "anet.companion"
    const val VERSION = 1

    const val OBSERVATION_BATCH = "companion.observation.batch"
    const val EPISODE = "companion.episode"
    const val INTERVENTION = "companion.intervention"
    const val USER_RESPONSE = "companion.user-response"
    const val APPROVAL_REQUEST = "companion.approval.request"
    const val APPROVAL_DECISION = "companion.approval.decision"

    private const val DAY_MS = 24L * 60L * 60L * 1000L
    private const val MAX_BODY_TTL_MS = 7L * DAY_MS
    private const val MAX_APPROVAL_REQUEST_TTL_MS = 15L * 60L * 1000L
    private const val MAX_APPROVAL_GRANT_TTL_MS = 60L * 60L * 1000L
    private const val MAX_CLOCK_SKEW_MS = 5L * 60L * 1000L

    private val idRegex = Regex("^[0-9a-f]{32}$")
    private val digestRegex = Regex("^[0-9a-f]{64}$")
    private val nodeIdRegex = Regex("^an1[a-z2-7]{17,}$")
    private val humanIdRegex = Regex("^hu1[a-z2-7]{17,}$")
    private val tokenRegex = Regex("^[a-z][a-z0-9_.-]{0,127}$")
    private val resourceRegex = Regex("^[a-zA-Z0-9][a-zA-Z0-9:/_.@-]{0,511}$")

    private val objectTypes = mapOf(
        OBSERVATION_BATCH to "observation_batch",
        EPISODE to "episode",
        INTERVENTION to "intervention",
        USER_RESPONSE to "user_response",
        APPROVAL_REQUEST to "approval_request",
        APPROVAL_DECISION to "approval_decision",
    )
    private val observationTypes = setOf(
        "device.battery",
        "device.network",
        "human.presence",
        "human.self-report",
        "device.app-category-window",
    )
    private val episodeContracts = mapOf(
        "device.battery-window" to ("device-essential" to "device.battery"),
        "device.connectivity-window" to ("device-essential" to "device.network"),
        "human.presence-window" to ("user-opt-in" to "human.presence"),
        "human.self-report" to ("user-initiated" to "human.self-report"),
        "device.app-category-window" to
            ("user-opt-in" to "device.app-category-window"),
    )
    private val forbiddenKeys = setOf(
        "audio",
        "audio_bytes",
        "image",
        "image_bytes",
        "video",
        "video_bytes",
        "raw",
        "raw_events",
        "precise_location",
        "latitude",
        "longitude",
        "chat_body",
        "message_body",
        "health_detail",
        "diagnosis",
        "emotion",
        "mood",
        "fatigue",
        "human_state",
        "state_hypothesis",
    )

    val json = Json {
        ignoreUnknownKeys = false
        isLenient = false
        explicitNulls = true
    }

    fun parseAndValidate(kind: String, encoded: String): JsonObject =
        validate(kind, json.parseToJsonElement(encoded))

    fun validate(kind: String, body: JsonElement): JsonObject =
        when (kind.trim().lowercase()) {
            OBSERVATION_BATCH -> validateObservationBatch(body)
            EPISODE -> validateEpisode(body)
            INTERVENTION -> validateIntervention(body)
            USER_RESPONSE -> validateUserResponse(body)
            APPROVAL_REQUEST -> validateApprovalRequest(body)
            APPROVAL_DECISION -> validateApprovalDecision(body)
            else -> fail("unsupported Companion kind")
        }

    fun canonicalJson(value: JsonElement): String = canonical(value).toString()

    fun validateApprovalDecisionBinding(
        request: JsonElement,
        decision: JsonElement,
    ): JsonObject {
        val normalizedRequest = validateApprovalRequest(request)
        val normalizedDecision = validateApprovalDecision(decision)
        for (field in listOf(
            "request_id",
            "human_id",
            "device_node_id",
            "nonce",
            "action",
            "scope",
        )) {
            requireValue(normalizedDecision[field] == normalizedRequest[field]) {
                "approval decision does not match request $field"
            }
        }
        val requestCreated = normalizedRequest.long("created_ms")
        val requestExpires = normalizedRequest.long("expires_ms")
        val decisionCreated = normalizedDecision.long("created_ms")
        requireValue(decisionCreated >= requestCreated) {
            "approval decision predates its request"
        }
        requireValue(decisionCreated < requestExpires) {
            "approval decision was created after request expiry"
        }
        return normalizedDecision
    }

    fun validateEndpointBinding(
        kind: String,
        body: JsonElement,
        senderNodeId: String,
        destinationNodeId: String,
        nowMs: Long,
    ): JsonObject {
        val normalizedKind = kind.trim().lowercase()
        val normalized = validate(normalizedKind, body)
        val sender = nodeId(senderNodeId, "Packet sender_node_id")
        val destination = nodeId(
            destinationNodeId,
            "Packet destination_node_id",
        )
        requireValue(nowMs > 0) { "invalid current time" }
        requireValue(normalized.long("created_ms") <= nowMs + MAX_CLOCK_SKEW_MS) {
            "Companion object creation time is too far in the future"
        }
        requireValue(normalized.long("expires_ms") > nowMs) {
            "Companion object expired"
        }
        val sourceField = when (normalizedKind) {
            OBSERVATION_BATCH, EPISODE -> "source_node_id"
            USER_RESPONSE, APPROVAL_DECISION -> "device_node_id"
            else -> null
        }
        val targetField = when (normalizedKind) {
            INTERVENTION -> "target_device_id"
            APPROVAL_REQUEST -> "device_node_id"
            else -> null
        }
        if (sourceField != null) {
            requireValue(normalized.string(sourceField) == sender) {
                "$normalizedKind $sourceField does not match Packet sender"
            }
        }
        if (targetField != null) {
            requireValue(normalized.string(targetField) == destination) {
                "$normalizedKind $targetField does not match Packet destination"
            }
        }
        return normalized
    }

    private fun validateObservationBatch(body: JsonElement): JsonObject {
        val value = base(
            OBSERVATION_BATCH,
            body,
            "batch_id",
            setOf(
                "source_node_id",
                "window_start_ms",
                "window_end_ms",
                "data_level",
                "consent",
                "observations",
            ),
        )
        val windowStart = positiveLong(value["window_start_ms"], "window_start_ms")
        val windowEnd = positiveLong(value["window_end_ms"], "window_end_ms")
        requireValue(
            windowEnd >= windowStart && windowEnd <= value.long("created_ms"),
        ) { "invalid observation window" }
        val raw = value.array("observations")
        requireValue(raw.isNotEmpty()) {
            "observations must be a non-empty list"
        }
        requireValue(raw.size <= 256) { "too many observations" }
        val observations = raw.map(::observation)
        requireUnique(
            observations.map { it.string("observation_id") },
            "duplicate observation_id",
        )
        requireValue(observations.all {
            it.long("observed_ms") in windowStart..windowEnd
        }) { "observation is outside its declared window" }
        val consent = consent(value.required("consent"))
        val types = observations.map { it.string("type") }.toSet()
        for (type in types) {
            val expected = when (type) {
                "human.self-report" -> "user-initiated"
                "human.presence", "device.app-category-window" -> "user-opt-in"
                else -> "device-essential"
            }
            requireValue(consent.string("basis") == expected) {
                "$type requires $expected consent basis"
            }
            requireValue(consent.strings("scope").contains(type)) {
                "consent scope does not cover observation type"
            }
        }
        return replace(
            value,
            "source_node_id" to JsonPrimitive(
                nodeId(value.string("source_node_id"), "source_node_id"),
            ),
            "window_start_ms" to JsonPrimitive(windowStart),
            "window_end_ms" to JsonPrimitive(windowEnd),
            "data_level" to JsonPrimitive(dataLevel(value.string("data_level"))),
            "consent" to consent,
            "observations" to JsonArray(observations),
        )
    }

    private fun observation(element: JsonElement): JsonObject {
        val item = exact(
            element,
            setOf("observation_id", "observed_ms", "type", "value"),
            "observation",
        )
        val type = item.string("type").trim().lowercase()
        requireValue(type in observationTypes) {
            "unsupported P0 observation type"
        }
        val normalized = buildJsonObject {
            put(
                "observation_id",
                identifier(item.string("observation_id"), "observation_id"),
            )
            put(
                "observed_ms",
                positiveLong(item["observed_ms"], "observed_ms"),
            )
            put("type", type)
            put("value", observationValue(type, item.required("value")))
        }
        rejectForbidden(normalized)
        return normalized
    }

    private fun observationValue(type: String, element: JsonElement): JsonObject =
        when (type) {
            "device.battery" -> {
                val item = exact(
                    element,
                    setOf("percent", "charging"),
                    "battery observation",
                )
                val percent = nonNegativeLong(item["percent"], "battery percent")
                requireValue(percent <= 100) { "invalid battery observation" }
                buildJsonObject {
                    put("percent", percent)
                    put("charging", item.boolean("charging"))
                }
            }
            "device.network" -> {
                val item = exact(
                    element,
                    setOf("transport", "metered"),
                    "network observation",
                )
                val transport = item.string("transport").trim().lowercase()
                requireValue(
                    transport in setOf(
                        "offline",
                        "wifi",
                        "cellular",
                        "ethernet",
                        "other",
                    ),
                ) { "invalid network transport" }
                buildJsonObject {
                    put("transport", transport)
                    put("metered", item.boolean("metered"))
                }
            }
            "human.presence" -> {
                val item = exact(element, setOf("state"), "presence observation")
                val state = item.string("state").trim().lowercase()
                requireValue(state in setOf("present", "away", "unknown")) {
                    "invalid presence state"
                }
                buildJsonObject { put("state", state) }
            }
            "human.self-report" -> {
                val item = exact(
                    element,
                    setOf("text", "format"),
                    "self-report observation",
                )
                requireValue(item.string("format") == "plain") {
                    "self-report format must be plain"
                }
                buildJsonObject {
                    put(
                        "text",
                        text(
                            item["text"],
                            "self-report text",
                            required = true,
                            limit = 2048,
                        ),
                    )
                    put("format", "plain")
                }
            }
            else -> {
                val item = exact(
                    element,
                    setOf("category_durations_ms", "switch_count"),
                    "app category window",
                )
                val durations = item.obj("category_durations_ms")
                requireValue(durations.isNotEmpty()) {
                    "app category durations must be a non-empty map"
                }
                requireValue(durations.size <= 32) { "too many app categories" }
                val normalized = durations.entries.associate { (key, value) ->
                    token(key, "app category") to
                        nonNegativeLong(value, "app category duration")
                }.toSortedMap()
                buildJsonObject {
                    put(
                        "category_durations_ms",
                        buildJsonObject {
                            normalized.forEach { (key, value) -> put(key, value) }
                        },
                    )
                    put(
                        "switch_count",
                        nonNegativeLong(item["switch_count"], "app switch count"),
                    )
                }
            }
        }

    private fun validateEpisode(body: JsonElement): JsonObject {
        val value = base(
            EPISODE,
            body,
            "episode_id",
            setOf(
                "source_node_id",
                "source_batch_ids",
                "episode_type",
                "window_start_ms",
                "window_end_ms",
                "data_level",
                "consent",
                "transform_version",
                "metrics",
            ),
        )
        val sourceIds = value.strings("source_batch_ids").map {
            identifier(it, "source batch ID")
        }
        requireValue(sourceIds.isNotEmpty() && sourceIds.size <= 64) {
            "invalid source_batch_ids"
        }
        requireUnique(sourceIds, "invalid source_batch_ids")
        val episodeType = value.string("episode_type").trim().lowercase()
        val contract = episodeContracts[episodeType]
            ?: fail("unsupported P0 episode type")
        val windowStart = positiveLong(value["window_start_ms"], "window_start_ms")
        val windowEnd = positiveLong(value["window_end_ms"], "window_end_ms")
        requireValue(
            windowEnd >= windowStart && windowEnd <= value.long("created_ms"),
        ) { "invalid episode window" }
        val rawMetrics = value.obj("metrics")
        requireValue(rawMetrics.size <= 64) { "invalid episode metrics" }
        val metrics = rawMetrics.entries.associate { (key, metric) ->
            token(key, "episode metric name") to metricValue(metric)
        }.toSortedMap()
        val normalizedMetrics = buildJsonObject {
            metrics.forEach { (key, metric) -> put(key, metric) }
        }
        rejectForbidden(normalizedMetrics, "metrics")
        val normalizedConsent = consent(value.required("consent"))
        requireValue(normalizedConsent.string("basis") == contract.first) {
            "$episodeType requires ${contract.first} consent basis"
        }
        requireValue(contract.second in normalizedConsent.strings("scope")) {
            "consent scope does not cover episode type"
        }
        return replace(
            value,
            "source_node_id" to JsonPrimitive(
                nodeId(value.string("source_node_id"), "source_node_id"),
            ),
            "source_batch_ids" to stringsArray(sourceIds),
            "episode_type" to JsonPrimitive(episodeType),
            "window_start_ms" to JsonPrimitive(windowStart),
            "window_end_ms" to JsonPrimitive(windowEnd),
            "data_level" to JsonPrimitive(dataLevel(value.string("data_level"))),
            "consent" to normalizedConsent,
            "transform_version" to JsonPrimitive(
                token(value.string("transform_version"), "transform_version"),
            ),
            "metrics" to normalizedMetrics,
        )
    }

    private fun validateIntervention(body: JsonElement): JsonObject {
        val value = base(
            INTERVENTION,
            body,
            "intervention_id",
            setOf(
                "human_id",
                "target_device_id",
                "category",
                "priority",
                "title",
                "message",
                "dedupe_key",
                "response_options",
                "related_episode_ids",
            ),
        )
        val category = value.string("category").trim().lowercase()
        requireValue(
            category in setOf("notification", "reminder", "choice", "report"),
        ) { "invalid intervention category" }
        val priority = value.string("priority").trim().lowercase()
        requireValue(priority in setOf("low", "normal", "high", "urgent")) {
            "invalid intervention priority"
        }
        val rawOptions = value.array("response_options")
        requireValue(rawOptions.size <= 16) { "too many response options" }
        val options = rawOptions.map {
            val item = exact(it, setOf("action_id", "label"), "response option")
            buildJsonObject {
                put("action_id", token(item.string("action_id"), "action_id"))
                put(
                    "label",
                    text(
                        item["label"],
                        "response option label",
                        required = true,
                        limit = 128,
                    ),
                )
            }
        }
        requireUnique(
            options.map { it.string("action_id") },
            "duplicate response action_id",
        )
        val related = value.strings("related_episode_ids").map {
            identifier(it, "related episode ID")
        }
        requireValue(related.size <= 64) { "invalid related_episode_ids" }
        requireUnique(related, "duplicate related episode ID")
        return replace(
            value,
            "human_id" to JsonPrimitive(humanId(value.string("human_id"))),
            "target_device_id" to JsonPrimitive(
                nodeId(value.string("target_device_id")),
            ),
            "category" to JsonPrimitive(category),
            "priority" to JsonPrimitive(priority),
            "title" to JsonPrimitive(
                text(
                    value["title"],
                    "intervention title",
                    required = true,
                    limit = 256,
                ),
            ),
            "message" to JsonPrimitive(
                text(
                    value["message"],
                    "intervention message",
                    required = true,
                ),
            ),
            "dedupe_key" to JsonPrimitive(
                identifier(value.string("dedupe_key"), "dedupe_key"),
            ),
            "response_options" to JsonArray(options),
            "related_episode_ids" to stringsArray(related),
        )
    }

    private fun validateUserResponse(body: JsonElement): JsonObject {
        val value = base(
            USER_RESPONSE,
            body,
            "response_id",
            setOf(
                "intervention_id",
                "human_id",
                "device_node_id",
                "disposition",
                "action_id",
                "text",
            ),
        )
        val disposition = value.string("disposition").trim().lowercase()
        requireValue(
            disposition in setOf(
                "presented",
                "ignored",
                "snoozed",
                "accepted",
                "rejected",
                "answered",
            ),
        ) { "invalid user response disposition" }
        val rawAction = value.string("action_id").trim().lowercase()
        val actionId = if (rawAction.isEmpty()) "" else token(rawAction, "action_id")
        val normalizedText = text(
            value["text"],
            "user response text",
            limit = 2048,
        )
        requireValue(
            disposition != "answered" ||
                actionId.isNotEmpty() ||
                normalizedText.isNotEmpty(),
        ) { "answered response requires action_id or text" }
        return replace(
            value,
            "intervention_id" to JsonPrimitive(
                identifier(
                    value.string("intervention_id"),
                    "intervention_id",
                ),
            ),
            "human_id" to JsonPrimitive(humanId(value.string("human_id"))),
            "device_node_id" to JsonPrimitive(
                nodeId(value.string("device_node_id")),
            ),
            "disposition" to JsonPrimitive(disposition),
            "action_id" to JsonPrimitive(actionId),
            "text" to JsonPrimitive(normalizedText),
        )
    }

    private fun validateApprovalRequest(body: JsonElement): JsonObject {
        val value = base(
            APPROVAL_REQUEST,
            body,
            "request_id",
            setOf(
                "human_id",
                "device_node_id",
                "action",
                "scope",
                "risk",
                "nonce",
            ),
            MAX_APPROVAL_REQUEST_TTL_MS,
        )
        return replace(
            value,
            "human_id" to JsonPrimitive(humanId(value.string("human_id"))),
            "device_node_id" to JsonPrimitive(
                nodeId(value.string("device_node_id")),
            ),
            "action" to approvalAction(value.required("action")),
            "scope" to approvalScope(
                value.required("scope"),
                value.long("created_ms"),
            ),
            "risk" to JsonPrimitive(
                text(
                    value["risk"],
                    "approval risk",
                    required = true,
                    limit = 1024,
                ),
            ),
            "nonce" to JsonPrimitive(
                identifier(value.string("nonce"), "approval nonce"),
            ),
        )
    }

    private fun validateApprovalDecision(body: JsonElement): JsonObject {
        val value = base(
            APPROVAL_DECISION,
            body,
            "decision_id",
            setOf(
                "request_id",
                "human_id",
                "device_node_id",
                "decision",
                "reason",
                "nonce",
                "action",
                "scope",
            ),
            MAX_APPROVAL_REQUEST_TTL_MS,
        )
        val decision = value.string("decision").trim().lowercase()
        requireValue(decision in setOf("approved", "rejected")) {
            "invalid approval decision"
        }
        return replace(
            value,
            "request_id" to JsonPrimitive(
                identifier(value.string("request_id"), "request_id"),
            ),
            "human_id" to JsonPrimitive(humanId(value.string("human_id"))),
            "device_node_id" to JsonPrimitive(
                nodeId(value.string("device_node_id")),
            ),
            "decision" to JsonPrimitive(decision),
            "reason" to JsonPrimitive(
                text(
                    value["reason"],
                    "approval decision reason",
                    limit = 1024,
                ),
            ),
            "nonce" to JsonPrimitive(
                identifier(value.string("nonce"), "approval nonce"),
            ),
            "action" to approvalAction(value.required("action")),
            "scope" to approvalScope(
                value.required("scope"),
                value.long("created_ms"),
            ),
        )
    }

    private fun approvalAction(element: JsonElement): JsonObject {
        val item = exact(
            element,
            setOf("capability", "resource", "parameters_digest", "summary"),
            "approval action",
        )
        val resource = item.string("resource").trim()
        requireValue(resourceRegex.matches(resource)) {
            "invalid approval resource"
        }
        return buildJsonObject {
            put(
                "capability",
                token(item.string("capability"), "approval capability"),
            )
            put("resource", resource)
            put(
                "parameters_digest",
                digest(
                    item.string("parameters_digest"),
                    "parameters_digest",
                ),
            )
            put(
                "summary",
                text(
                    item["summary"],
                    "approval action summary",
                    required = true,
                    limit = 512,
                ),
            )
        }
    }

    private fun approvalScope(
        element: JsonElement,
        createdMs: Long,
    ): JsonObject {
        val item = exact(
            element,
            setOf("mode", "max_uses", "grant_expires_ms"),
            "approval scope",
        )
        val mode = item.string("mode").trim().lowercase()
        requireValue(mode in setOf("once", "bounded")) {
            "approval scope mode must be once or bounded"
        }
        val maxUses = positiveLong(item["max_uses"], "approval max_uses")
        val grantExpires = positiveLong(
            item["grant_expires_ms"],
            "grant_expires_ms",
        )
        requireValue(
            grantExpires > createdMs &&
                grantExpires - createdMs <= MAX_APPROVAL_GRANT_TTL_MS,
        ) { "invalid approval grant lifetime" }
        requireValue(mode != "once" || maxUses == 1L) {
            "once approval must have max_uses=1"
        }
        requireValue(mode != "bounded" || maxUses <= 100L) {
            "bounded approval max_uses exceeds limit"
        }
        return buildJsonObject {
            put("mode", mode)
            put("max_uses", maxUses)
            put("grant_expires_ms", grantExpires)
        }
    }

    private fun base(
        kind: String,
        body: JsonElement,
        objectIdName: String,
        additionalFields: Set<String>,
        maxTtlMs: Long = MAX_BODY_TTL_MS,
    ): JsonObject {
        val objectType = objectTypes[kind] ?: fail("unsupported Companion kind")
        val required = setOf(
            "protocol",
            "version",
            "object_type",
            objectIdName,
            "created_ms",
            "expires_ms",
        ) + additionalFields
        val value = exact(body, required, objectType)
        requireValue(value.string("protocol") == PROTOCOL) {
            "unsupported Companion protocol"
        }
        requireValue(value.long("version") == VERSION.toLong()) {
            "unsupported Companion protocol version"
        }
        requireValue(value.string("object_type") == objectType) {
            "Companion kind/object_type mismatch"
        }
        identifier(value.string(objectIdName), objectIdName)
        val created = positiveLong(value["created_ms"], "created_ms")
        val expires = positiveLong(value["expires_ms"], "expires_ms")
        requireValue(expires > created && expires - created <= maxTtlMs) {
            "invalid object lifetime"
        }
        return value
    }

    private fun consent(element: JsonElement): JsonObject {
        val item = exact(
            element,
            setOf("basis", "grant_id", "scope"),
            "consent",
        )
        val basis = item.string("basis").trim().lowercase()
        requireValue(
            basis in setOf("device-essential", "user-opt-in", "user-initiated"),
        ) { "invalid consent basis" }
        val rawGrant = item.string("grant_id").trim().lowercase()
        val grantId = if (basis == "user-opt-in") {
            identifier(rawGrant, "consent grant_id")
        } else {
            requireValue(rawGrant.isEmpty()) {
                "only user-opt-in consent may carry grant_id"
            }
            ""
        }
        val scope = item.strings("scope").map {
            token(it, "consent scope")
        }.toSortedSet().toList()
        requireValue(scope.isNotEmpty()) {
            "consent scope must be a non-empty list"
        }
        requireValue(scope.size <= 32) { "too many consent scopes" }
        return buildJsonObject {
            put("basis", basis)
            put("grant_id", grantId)
            put("scope", stringsArray(scope))
        }
    }

    private fun metricValue(element: JsonElement): JsonElement = when (element) {
        JsonNull -> JsonNull
        is JsonArray -> {
            requireValue(element.size <= 32) {
                "episode metric list is too long"
            }
            JsonArray(element.map(::metricValue))
        }
        is JsonObject -> fail(
            "episode metrics must contain only bounded scalar values",
        )
        is JsonPrimitive -> {
            if (element.isString) {
                JsonPrimitive(text(element, "episode metric", limit = 512))
            } else if (element.content == "true" || element.content == "false") {
                JsonPrimitive(element.content.toBooleanStrict())
            } else {
                val integer = element.content.toLongOrNull()
                if (integer != null) {
                    JsonPrimitive(integer)
                } else {
                    val number = element.content.toDoubleOrNull()
                        ?: fail(
                            "episode metrics must contain only bounded scalar values",
                        )
                    requireValue(number.isFinite()) {
                        "episode metric numbers must be finite"
                    }
                    JsonPrimitive(number)
                }
            }
        }
    }

    private fun rejectForbidden(
        element: JsonElement,
        path: String = "value",
    ) {
        when (element) {
            is JsonObject -> element.forEach { (key, value) ->
                val normalized = key.trim().lowercase()
                requireValue(normalized !in forbiddenKeys) {
                    "forbidden Companion field: $path.$normalized"
                }
                rejectForbidden(value, "$path.$normalized")
            }
            is JsonArray -> element.forEachIndexed { index, value ->
                rejectForbidden(value, "$path[$index]")
            }
            else -> Unit
        }
    }

    private fun exact(
        element: JsonElement,
        required: Set<String>,
        name: String,
    ): JsonObject {
        val value = element as? JsonObject ?: fail("$name must be a map")
        val missing = required - value.keys
        val unknown = value.keys - required
        requireValue(missing.isEmpty()) {
            "$name is missing fields: ${missing.sorted().joinToString()}"
        }
        requireValue(unknown.isEmpty()) {
            "$name has unknown fields: ${unknown.sorted().joinToString()}"
        }
        return value
    }

    private fun identifier(value: String, name: String): String {
        val normalized = value.trim().lowercase()
        requireValue(idRegex.matches(normalized)) {
            "$name must be 32 lowercase hexadecimal characters"
        }
        return normalized
    }

    private fun digest(value: String, name: String): String {
        val normalized = value.trim().lowercase()
        requireValue(digestRegex.matches(normalized)) {
            "$name must be 64 lowercase hexadecimal characters"
        }
        return normalized
    }

    private fun nodeId(value: String, name: String = "device_node_id"): String {
        val normalized = value.trim().lowercase()
        requireValue(nodeIdRegex.matches(normalized)) { "invalid $name" }
        return normalized
    }

    private fun humanId(value: String): String {
        val normalized = value.trim().lowercase()
        requireValue(humanIdRegex.matches(normalized)) { "invalid human_id" }
        return normalized
    }

    private fun token(value: String, name: String): String {
        val normalized = value.trim().lowercase()
        requireValue(tokenRegex.matches(normalized)) { "invalid $name" }
        return normalized
    }

    private fun dataLevel(value: String): String {
        val normalized = value.trim().lowercase()
        requireValue(normalized in setOf("operational", "personal-low")) {
            "P0 Companion only permits operational or personal-low data"
        }
        return normalized
    }

    private fun text(
        element: JsonElement?,
        name: String,
        required: Boolean = false,
        limit: Int = 4096,
    ): String {
        val value = element as? JsonPrimitive
            ?: fail("$name must be text")
        requireValue(value.isString) { "$name must be text" }
        val normalized = value.content.trim()
        requireValue(!required || normalized.isNotEmpty()) {
            "$name is required"
        }
        requireValue(normalized.length <= limit) { "$name is too long" }
        return normalized
    }

    private fun positiveLong(element: JsonElement?, name: String): Long {
        val value = strictLong(element, name)
        requireValue(value >= 1) { "invalid $name" }
        return value
    }

    private fun nonNegativeLong(element: JsonElement?, name: String): Long {
        val value = strictLong(element, name)
        requireValue(value >= 0) { "invalid $name" }
        return value
    }

    private fun strictLong(element: JsonElement?, name: String): Long {
        val primitive = element as? JsonPrimitive ?: fail("invalid $name")
        requireValue(!primitive.isString) { "invalid $name" }
        return primitive.content.toLongOrNull() ?: fail("invalid $name")
    }

    private fun JsonObject.required(name: String): JsonElement =
        this[name] ?: fail("missing $name")

    private fun JsonObject.string(name: String): String {
        val primitive = this[name] as? JsonPrimitive ?: fail("invalid $name")
        requireValue(primitive.isString) { "invalid $name" }
        return primitive.content
    }

    private fun JsonObject.long(name: String): Long = strictLong(this[name], name)

    private fun JsonObject.boolean(name: String): Boolean {
        val primitive = this[name] as? JsonPrimitive ?: fail("invalid $name")
        requireValue(!primitive.isString) { "invalid $name" }
        return when (primitive.content) {
            "true" -> true
            "false" -> false
            else -> fail("invalid $name")
        }
    }

    private fun JsonObject.obj(name: String): JsonObject =
        this[name] as? JsonObject ?: fail("$name must be a map")

    private fun JsonObject.array(name: String): JsonArray =
        this[name] as? JsonArray ?: fail("$name must be a list")

    private fun JsonObject.strings(name: String): List<String> =
        array(name).map {
            val primitive = it as? JsonPrimitive ?: fail("invalid $name")
            requireValue(primitive.isString) { "invalid $name" }
            primitive.content
        }

    private fun stringsArray(values: List<String>): JsonArray =
        buildJsonArray { values.forEach { add(JsonPrimitive(it)) } }

    private fun replace(
        original: JsonObject,
        vararg replacements: Pair<String, JsonElement>,
    ): JsonObject {
        val changes = replacements.toMap()
        return buildJsonObject {
            original.forEach { (key, value) -> put(key, changes[key] ?: value) }
        }
    }

    private fun canonical(element: JsonElement): JsonElement = when (element) {
        is JsonObject -> buildJsonObject {
            element.keys.sorted().forEach { key ->
                put(key, canonical(element.getValue(key)))
            }
        }
        is JsonArray -> JsonArray(element.map(::canonical))
        else -> element
    }

    private fun requireUnique(values: List<String>, message: String) {
        requireValue(values.size == values.toSet().size) { message }
    }

    private inline fun requireValue(
        condition: Boolean,
        lazyMessage: () -> String,
    ) {
        if (!condition) {
            throw CompanionValidationException(lazyMessage())
        }
    }

    private fun fail(message: String): Nothing =
        throw CompanionValidationException(message)
}

class CompanionValidationException(message: String) : IllegalArgumentException(message)

package io.anet.companion.app.data

import androidx.room.withTransaction
import io.anet.companion.CompanionProtocol
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import java.security.MessageDigest
import java.util.UUID

data class OutboxClaim(
    val semanticId: String,
    val kind: String,
    val claimToken: String,
    val body: String,
    val expiresMs: Long,
    val attempts: Int,
)

data class CompanionLocalStatus(
    val pendingOutbox: Int,
    val openInterventions: Int,
)

class CompanionRepository(
    private val database: CompanionDatabase,
    private val cipher: PayloadCipher,
) {
    private val dao = database.companionDao()

    suspend fun installConsent(
        grantId: String,
        scopes: Set<String>,
        expiresMs: Long,
        nowMs: Long,
    ): Boolean = database.withTransaction {
        val normalizedId = localIdentifier(grantId, "consent grant ID")
        require(expiresMs > nowMs) { "consent grant must expire in the future" }
        require(expiresMs - nowMs <= MAX_CONSENT_TTL_MS) {
            "consent grant lifetime exceeds local limit"
        }
        val normalizedScopes = scopes.map {
            it.trim().lowercase()
        }.toSortedSet()
        require(normalizedScopes.isNotEmpty()) { "consent scope is empty" }
        require(normalizedScopes.all { it in USER_OPT_IN_SCOPES }) {
            "consent contains an unsupported P0 scope"
        }
        val encodedScopes = normalizedScopes.joinToString("\n")
        val existing = dao.consent(normalizedId)
        if (existing != null) {
            require(
                existing.scopes == encodedScopes &&
                    existing.expiresMs == expiresMs &&
                    existing.state == "active",
            ) { "consent grant ID conflicts with existing state" }
            return@withTransaction false
        }
        dao.insertConsent(
            ConsentGrantEntity(
                grantId = normalizedId,
                scopes = encodedScopes,
                createdMs = nowMs,
                expiresMs = expiresMs,
                state = "active",
                withdrawnMs = 0,
            ),
        )
        true
    }

    suspend fun withdrawConsent(grantId: String, nowMs: Long): Int =
        database.withTransaction {
            val normalizedId = localIdentifier(grantId, "consent grant ID")
            val changed = dao.withdrawConsent(normalizedId, nowMs)
            if (changed == 0) {
                val existing = dao.consent(normalizedId)
                    ?: throw IllegalArgumentException("unknown consent grant")
                require(existing.state == "withdrawn") {
                    "consent grant is not active"
                }
                return@withTransaction 0
            }
            dao.deleteUnsentForConsent(normalizedId)
        }

    suspend fun queueOutbound(
        kind: String,
        encodedBody: String,
        senderNodeId: String,
        destinationNodeId: String,
        nowMs: Long,
        userInitiated: Boolean = false,
    ): Boolean = database.withTransaction {
        val normalized = CompanionProtocol.validateEndpointBinding(
            kind,
            CompanionProtocol.json.parseToJsonElement(encodedBody),
            senderNodeId,
            destinationNodeId,
            nowMs,
        )
        queueValidated(
            kind.trim().lowercase(),
            normalized,
            nowMs,
            userInitiated,
        )
    }

    suspend fun claimOutbound(
        owner: String,
        limit: Int,
        leaseMs: Long,
        nowMs: Long,
    ): List<OutboxClaim> = database.withTransaction {
        val normalizedOwner = workerName(owner)
        require(limit in 1..100) { "claim limit must be between 1 and 100" }
        require(leaseMs in 5_000L..86_400_000L) {
            "claim lease is outside the allowed range"
        }
        dao.deleteExpiredOutbox(nowMs)
        dao.recoverExpiredLeases(nowMs)
        val result = mutableListOf<OutboxClaim>()
        for (semanticId in dao.readyOutboxIds(nowMs, limit)) {
            val claimToken = randomIdentifier()
            if (
                dao.leaseOutbox(
                    semanticId,
                    normalizedOwner,
                    claimToken,
                    nowMs + leaseMs,
                    nowMs,
                ) != 1
            ) {
                continue
            }
            val row = requireNotNull(dao.claimedOutbox(claimToken))
            val plaintext = cipher.decrypt(
                EncryptedPayload(row.ciphertext, row.iv),
                row.aad,
            )
            result += OutboxClaim(
                semanticId = row.semanticId,
                kind = row.kind,
                claimToken = claimToken,
                body = plaintext.toString(Charsets.UTF_8),
                expiresMs = row.expiresMs,
                attempts = row.attempts,
            )
        }
        result
    }

    suspend fun settleOutbound(
        claimToken: String,
        sent: Boolean,
        nowMs: Long,
        retryAfterMs: Long = nowMs,
    ) {
        val normalizedToken = localIdentifier(claimToken, "claim token")
        val changed = if (sent) {
            dao.markOutboxSent(normalizedToken, nowMs)
        } else {
            require(retryAfterMs in nowMs..(nowMs + MAX_RETRY_MS)) {
                "retry time is outside the allowed range"
            }
            dao.retryOutbox(normalizedToken, retryAfterMs, nowMs)
        }
        require(changed == 1) { "outbox claim is stale or fenced" }
    }

    suspend fun acceptIntervention(
        encodedBody: String,
        senderNodeId: String,
        localDeviceNodeId: String,
        localHumanId: String,
        nowMs: Long,
    ): Boolean = database.withTransaction {
        val normalized = CompanionProtocol.validateEndpointBinding(
            CompanionProtocol.INTERVENTION,
            CompanionProtocol.json.parseToJsonElement(encodedBody),
            senderNodeId,
            localDeviceNodeId,
            nowMs,
        )
        require(normalized.string("human_id") == localHumanId) {
            "intervention human does not match local principal"
        }
        val interventionId = normalized.string("intervention_id")
        val dedupeKey = normalized.string("dedupe_key")
        val canonical = CompanionProtocol.canonicalJson(normalized)
        val bodyHash = sha256(canonical.toByteArray(Charsets.UTF_8))
        val existing = dao.intervention(interventionId)
            ?: dao.interventionByDedupe(dedupeKey)
        if (existing != null) {
            require(
                existing.interventionId == interventionId &&
                    existing.dedupeKey == dedupeKey &&
                    existing.bodyHash == bodyHash,
            ) { "intervention ID or dedupe key conflicts with another body" }
            return@withTransaction false
        }
        val aad = aad(
            "intervention",
            interventionId,
            CompanionProtocol.INTERVENTION,
            normalized.long("created_ms"),
            normalized.long("expires_ms"),
        )
        val encrypted = cipher.encrypt(
            canonical.toByteArray(Charsets.UTF_8),
            aad,
        )
        dao.insertIntervention(
            InterventionEntity(
                interventionId = interventionId,
                dedupeKey = dedupeKey,
                bodyHash = bodyHash,
                ciphertext = encrypted.ciphertext,
                iv = encrypted.iv,
                aad = aad,
                humanId = normalized.string("human_id"),
                targetDeviceId = normalized.string("target_device_id"),
                createdMs = normalized.long("created_ms"),
                expiresMs = normalized.long("expires_ms"),
                state = "received",
                responseId = "",
                receivedMs = nowMs,
                updatedMs = nowMs,
            ),
        )
        true
    }

    suspend fun markInterventionPresented(
        interventionId: String,
        nowMs: Long,
    ): Boolean {
        val normalizedId = localIdentifier(interventionId, "intervention ID")
        val changed = dao.markInterventionPresented(normalizedId, nowMs)
        if (changed == 1) {
            return true
        }
        val existing = dao.intervention(normalizedId)
            ?: throw IllegalArgumentException("unknown intervention")
        require(existing.state in setOf("presented", "responded")) {
            "intervention cannot be presented"
        }
        return false
    }

    suspend fun recordUserResponse(
        encodedBody: String,
        localDeviceNodeId: String,
        destinationNodeId: String,
        localHumanId: String,
        nowMs: Long,
    ): Boolean = database.withTransaction {
        val normalized = CompanionProtocol.validateEndpointBinding(
            CompanionProtocol.USER_RESPONSE,
            CompanionProtocol.json.parseToJsonElement(encodedBody),
            localDeviceNodeId,
            destinationNodeId,
            nowMs,
        )
        require(normalized.string("human_id") == localHumanId) {
            "response human does not match local principal"
        }
        val interventionId = normalized.string("intervention_id")
        val interventionRow = dao.intervention(interventionId)
            ?: throw IllegalArgumentException(
                "response has no local intervention",
            )
        require(interventionRow.humanId == localHumanId) {
            "response human does not match intervention"
        }
        require(interventionRow.targetDeviceId == localDeviceNodeId) {
            "response device does not match intervention"
        }
        val intervention = decryptIntervention(interventionRow)
        val actionId = normalized.string("action_id")
        if (actionId.isNotEmpty()) {
            val allowed = intervention["response_options"]!!
                .jsonArray
                .map { it.jsonObject.string("action_id") }
                .toSet()
            require(actionId in allowed) {
                "response action is not offered by the intervention"
            }
        }
        val responseId = normalized.string("response_id")
        if (interventionRow.responseId.isNotEmpty()) {
            require(interventionRow.responseId == responseId) {
                "intervention already has another response"
            }
            val existing = dao.outbox(responseId)
                ?: throw IllegalStateException("response ledger is incomplete")
            require(existing.bodyHash == bodyHash(normalized)) {
                "response ID conflicts with another body"
            }
            return@withTransaction false
        }
        queueValidated(
            CompanionProtocol.USER_RESPONSE,
            normalized,
            nowMs,
            userInitiated = true,
        )
        require(
            dao.markInterventionResponded(
                interventionId,
                responseId,
                nowMs,
            ) == 1,
        ) { "intervention response state changed concurrently" }
        true
    }

    suspend fun localStatus(): CompanionLocalStatus =
        CompanionLocalStatus(
            pendingOutbox = dao.pendingOutboxCount(),
            openInterventions = dao.openInterventionCount(),
        )

    private suspend fun queueValidated(
        kind: String,
        normalized: JsonObject,
        nowMs: Long,
        userInitiated: Boolean,
    ): Boolean {
        val semanticId = semanticId(kind, normalized)
        val consentGrantId = requireLocalConsent(
            kind,
            normalized,
            nowMs,
            userInitiated,
        )
        val canonical = CompanionProtocol.canonicalJson(normalized)
        val bodyHash = sha256(canonical.toByteArray(Charsets.UTF_8))
        val existing = dao.outbox(semanticId)
        if (existing != null) {
            require(existing.kind == kind && existing.bodyHash == bodyHash) {
                "semantic ID conflicts with another outbound body"
            }
            return false
        }
        val createdMs = normalized.long("created_ms")
        val expiresMs = normalized.long("expires_ms")
        val aad = aad("outbox", semanticId, kind, createdMs, expiresMs)
        val encrypted = cipher.encrypt(
            canonical.toByteArray(Charsets.UTF_8),
            aad,
        )
        dao.insertOutbox(
            OutboxEntity(
                semanticId = semanticId,
                kind = kind,
                bodyHash = bodyHash,
                ciphertext = encrypted.ciphertext,
                iv = encrypted.iv,
                aad = aad,
                createdMs = createdMs,
                expiresMs = expiresMs,
                consentGrantId = consentGrantId,
                state = "pending",
                owner = "",
                claimToken = "",
                leaseUntilMs = 0,
                retryAfterMs = 0,
                attempts = 0,
                updatedMs = nowMs,
            ),
        )
        return true
    }

    private suspend fun requireLocalConsent(
        kind: String,
        body: JsonObject,
        nowMs: Long,
        userInitiated: Boolean,
    ): String {
        if (
            kind != CompanionProtocol.OBSERVATION_BATCH &&
            kind != CompanionProtocol.EPISODE
        ) {
            return ""
        }
        val consent = body["consent"]!!.jsonObject
        return when (consent.string("basis")) {
            "device-essential" -> ""
            "user-initiated" -> {
                require(userInitiated) {
                    "user-initiated observation requires a local user action"
                }
                ""
            }
            "user-opt-in" -> {
                val grantId = consent.string("grant_id")
                val grant = dao.consent(grantId)
                    ?: throw SecurityException("local consent grant is missing")
                require(grant.state == "active" && grant.expiresMs > nowMs) {
                    "local consent grant is not active"
                }
                val localScopes = grant.scopes.split("\n").toSet()
                val messageScopes = consent["scope"]!!
                    .jsonArray
                    .map { it.jsonPrimitive.content }
                    .toSet()
                require(localScopes.containsAll(messageScopes)) {
                    "message scope exceeds local consent"
                }
                grantId
            }
            else -> throw SecurityException("unsupported local consent basis")
        }
    }

    private fun decryptIntervention(row: InterventionEntity): JsonObject {
        val plaintext = cipher.decrypt(
            EncryptedPayload(row.ciphertext, row.iv),
            row.aad,
        )
        return CompanionProtocol.parseAndValidate(
            CompanionProtocol.INTERVENTION,
            plaintext.toString(Charsets.UTF_8),
        )
    }

    private fun semanticId(kind: String, body: JsonObject): String =
        body.string(
            when (kind) {
                CompanionProtocol.OBSERVATION_BATCH -> "batch_id"
                CompanionProtocol.EPISODE -> "episode_id"
                CompanionProtocol.INTERVENTION -> "intervention_id"
                CompanionProtocol.USER_RESPONSE -> "response_id"
                CompanionProtocol.APPROVAL_REQUEST -> "request_id"
                CompanionProtocol.APPROVAL_DECISION -> "decision_id"
                else -> throw IllegalArgumentException(
                    "unsupported Companion kind",
                )
            },
        )

    private fun bodyHash(body: JsonObject): String =
        sha256(
            CompanionProtocol.canonicalJson(body)
                .toByteArray(Charsets.UTF_8),
        )

    private fun aad(
        recordType: String,
        semanticId: String,
        kind: String,
        createdMs: Long,
        expiresMs: Long,
    ): ByteArray = listOf(
        "anet.companion.local.v1",
        recordType,
        semanticId,
        kind,
        createdMs.toString(),
        expiresMs.toString(),
    ).joinToString("\u0000").toByteArray(Charsets.UTF_8)

    private fun sha256(value: ByteArray): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value)
            .joinToString("") { "%02x".format(it) }

    private fun JsonObject.string(name: String): String =
        requireNotNull(this[name]).jsonPrimitive.content

    private fun JsonObject.long(name: String): Long =
        requireNotNull(this[name]).jsonPrimitive.long

    private fun localIdentifier(value: String, name: String): String {
        val normalized = value.trim().lowercase()
        require(IDENTIFIER.matches(normalized)) {
            "$name must be 32 lowercase hexadecimal characters"
        }
        return normalized
    }

    private fun randomIdentifier(): String =
        UUID.randomUUID().toString().replace("-", "")

    private fun workerName(value: String): String {
        val normalized = value.trim()
        require(WORKER.matches(normalized)) { "invalid worker name" }
        return normalized
    }

    private companion object {
        val IDENTIFIER = Regex("^[0-9a-f]{32}$")
        val WORKER = Regex("^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
        val USER_OPT_IN_SCOPES = setOf(
            "human.presence",
            "device.app-category-window",
        )
        const val MAX_CONSENT_TTL_MS = 365L * 24L * 60L * 60L * 1000L
        const val MAX_RETRY_MS = 24L * 60L * 60L * 1000L
    }
}

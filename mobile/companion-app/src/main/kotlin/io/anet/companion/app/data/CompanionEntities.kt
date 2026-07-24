package io.anet.companion.app.data

import androidx.room.Entity
import androidx.room.Index
import androidx.room.PrimaryKey

@Entity(tableName = "consent_grants")
data class ConsentGrantEntity(
    @PrimaryKey val grantId: String,
    val scopes: String,
    val createdMs: Long,
    val expiresMs: Long,
    val state: String,
    val withdrawnMs: Long,
)

@Entity(
    tableName = "outbox",
    indices = [
        Index(value = ["state", "retryAfterMs", "leaseUntilMs", "createdMs"]),
        Index(value = ["claimToken"], unique = true),
        Index(value = ["consentGrantId"]),
    ],
)
data class OutboxEntity(
    @PrimaryKey val semanticId: String,
    val kind: String,
    val bodyHash: String,
    val ciphertext: ByteArray,
    val iv: ByteArray,
    val aad: ByteArray,
    val createdMs: Long,
    val expiresMs: Long,
    val consentGrantId: String,
    val state: String,
    val owner: String,
    val claimToken: String,
    val leaseUntilMs: Long,
    val retryAfterMs: Long,
    val attempts: Int,
    val updatedMs: Long,
)

@Entity(
    tableName = "interventions",
    indices = [Index(value = ["dedupeKey"], unique = true)],
)
data class InterventionEntity(
    @PrimaryKey val interventionId: String,
    val dedupeKey: String,
    val bodyHash: String,
    val ciphertext: ByteArray,
    val iv: ByteArray,
    val aad: ByteArray,
    val humanId: String,
    val targetDeviceId: String,
    val createdMs: Long,
    val expiresMs: Long,
    val state: String,
    val responseId: String,
    val receivedMs: Long,
    val updatedMs: Long,
)

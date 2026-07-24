package io.anet.companion.app.data

import androidx.room.Dao
import androidx.room.Insert
import androidx.room.OnConflictStrategy
import androidx.room.Query

@Dao
interface CompanionDao {
    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insertConsent(value: ConsentGrantEntity)

    @Query("SELECT * FROM consent_grants WHERE grantId = :grantId")
    suspend fun consent(grantId: String): ConsentGrantEntity?

    @Query(
        """
        UPDATE consent_grants
        SET state = 'withdrawn', withdrawnMs = :withdrawnMs
        WHERE grantId = :grantId AND state = 'active'
        """,
    )
    suspend fun withdrawConsent(grantId: String, withdrawnMs: Long): Int

    @Query(
        """
        DELETE FROM outbox
        WHERE consentGrantId = :grantId
          AND state IN ('pending', 'leased', 'retry')
        """,
    )
    suspend fun deleteUnsentForConsent(grantId: String): Int

    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insertOutbox(value: OutboxEntity)

    @Query("SELECT * FROM outbox WHERE semanticId = :semanticId")
    suspend fun outbox(semanticId: String): OutboxEntity?

    @Query(
        """
        UPDATE outbox
        SET state = 'pending', owner = '', claimToken = '', leaseUntilMs = 0,
            updatedMs = :nowMs
        WHERE state = 'leased' AND leaseUntilMs <= :nowMs
        """,
    )
    suspend fun recoverExpiredLeases(nowMs: Long): Int

    @Query(
        """
        DELETE FROM outbox
        WHERE expiresMs <= :nowMs AND state != 'sent'
        """,
    )
    suspend fun deleteExpiredOutbox(nowMs: Long): Int

    @Query(
        """
        SELECT semanticId FROM outbox
        WHERE state IN ('pending', 'retry')
          AND retryAfterMs <= :nowMs AND expiresMs > :nowMs
        ORDER BY createdMs, semanticId
        LIMIT :limit
        """,
    )
    suspend fun readyOutboxIds(nowMs: Long, limit: Int): List<String>

    @Query(
        """
        UPDATE outbox
        SET state = 'leased', owner = :owner, claimToken = :claimToken,
            leaseUntilMs = :leaseUntilMs, attempts = attempts + 1,
            updatedMs = :nowMs
        WHERE semanticId = :semanticId
          AND state IN ('pending', 'retry')
          AND retryAfterMs <= :nowMs AND expiresMs > :nowMs
        """,
    )
    suspend fun leaseOutbox(
        semanticId: String,
        owner: String,
        claimToken: String,
        leaseUntilMs: Long,
        nowMs: Long,
    ): Int

    @Query("SELECT * FROM outbox WHERE claimToken = :claimToken AND state = 'leased'")
    suspend fun claimedOutbox(claimToken: String): OutboxEntity?

    @Query(
        """
        UPDATE outbox
        SET state = 'sent', owner = '', claimToken = '', leaseUntilMs = 0,
            retryAfterMs = 0, updatedMs = :nowMs
        WHERE claimToken = :claimToken AND state = 'leased'
        """,
    )
    suspend fun markOutboxSent(claimToken: String, nowMs: Long): Int

    @Query(
        """
        UPDATE outbox
        SET state = 'retry', owner = '', claimToken = '', leaseUntilMs = 0,
            retryAfterMs = :retryAfterMs, updatedMs = :nowMs
        WHERE claimToken = :claimToken AND state = 'leased'
        """,
    )
    suspend fun retryOutbox(
        claimToken: String,
        retryAfterMs: Long,
        nowMs: Long,
    ): Int

    @Insert(onConflict = OnConflictStrategy.ABORT)
    suspend fun insertIntervention(value: InterventionEntity)

    @Query("SELECT * FROM interventions WHERE interventionId = :interventionId")
    suspend fun intervention(interventionId: String): InterventionEntity?

    @Query("SELECT * FROM interventions WHERE dedupeKey = :dedupeKey")
    suspend fun interventionByDedupe(dedupeKey: String): InterventionEntity?

    @Query(
        """
        UPDATE interventions
        SET state = 'presented', updatedMs = :nowMs
        WHERE interventionId = :interventionId AND state = 'received'
        """,
    )
    suspend fun markInterventionPresented(
        interventionId: String,
        nowMs: Long,
    ): Int

    @Query(
        """
        UPDATE interventions
        SET state = 'responded', responseId = :responseId, updatedMs = :nowMs
        WHERE interventionId = :interventionId
          AND state IN ('received', 'presented')
          AND responseId = ''
        """,
    )
    suspend fun markInterventionResponded(
        interventionId: String,
        responseId: String,
        nowMs: Long,
    ): Int

    @Query("SELECT COUNT(*) FROM outbox WHERE state IN ('pending', 'retry', 'leased')")
    suspend fun pendingOutboxCount(): Int

    @Query("SELECT COUNT(*) FROM interventions WHERE state IN ('received', 'presented')")
    suspend fun openInterventionCount(): Int
}

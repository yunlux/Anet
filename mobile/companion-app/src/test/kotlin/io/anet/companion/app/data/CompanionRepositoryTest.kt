package io.anet.companion.app.data

import android.content.Context
import androidx.room.Room
import androidx.test.core.app.ApplicationProvider
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.security.SecureRandom
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class CompanionRepositoryTest {
    private lateinit var database: CompanionDatabase
    private lateinit var repository: CompanionRepository
    private lateinit var cipher: JvmPayloadCipher

    @Before
    fun setUp() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        database = Room.inMemoryDatabaseBuilder(
            context,
            CompanionDatabase::class.java,
        ).allowMainThreadQueries().build()
        cipher = JvmPayloadCipher()
        repository = CompanionRepository(database, cipher)
    }

    @After
    fun tearDown() {
        database.close()
    }

    @Test
    fun encryptedOutboxIsIdempotentAndLeaseFenced() = runBlocking {
        val body = fixture("observation-batch.json")
        assertTrue(
            repository.queueOutbound(
                OBSERVATION_KIND,
                body,
                DEVICE_ID,
                REMOTE_ID,
                NOW_MS,
            ),
        )
        assertFalse(
            repository.queueOutbound(
                OBSERVATION_KIND,
                body,
                DEVICE_ID,
                REMOTE_ID,
                NOW_MS,
            ),
        )

        val row = requireNotNull(
            database.companionDao().outbox(OBSERVATION_ID),
        )
        assertNotEquals(
            body,
            row.ciphertext.toString(Charsets.UTF_8),
        )
        assertFalse(
            row.ciphertext.toString(Charsets.ISO_8859_1)
                .contains("device.network"),
        )

        val first = repository.claimOutbound(
            "sender-a",
            1,
            5_000,
            NOW_MS,
        ).single()
        assertTrue(first.body.contains("\"batch_id\":\"$OBSERVATION_ID\""))
        assertEquals(1, first.attempts)

        val second = repository.claimOutbound(
            "sender-b",
            1,
            5_000,
            NOW_MS + 5_001,
        ).single()
        assertEquals(2, second.attempts)
        assertNotEquals(first.claimToken, second.claimToken)
        assertThrows(IllegalArgumentException::class.java) {
            runBlocking {
                repository.settleOutbound(
                    first.claimToken,
                    sent = true,
                    nowMs = NOW_MS + 5_002,
                )
            }
        }
        repository.settleOutbound(
            second.claimToken,
            sent = true,
            nowMs = NOW_MS + 5_002,
        )
        assertEquals(
            "sent",
            database.companionDao().outbox(OBSERVATION_ID)?.state,
        )
    }

    @Test
    fun consentWithdrawalDeletesUnsentDataAndBlocksReuse() = runBlocking {
        val grantId = "abababababababababababababababab"
        val body = optInObservation(grantId)
        assertTrue(
            repository.installConsent(
                grantId,
                setOf("human.presence"),
                NOW_MS + 60_000,
                NOW_MS,
            ),
        )
        assertTrue(
            repository.queueOutbound(
                OBSERVATION_KIND,
                body,
                DEVICE_ID,
                REMOTE_ID,
                NOW_MS,
            ),
        )
        repository.claimOutbound(
            "sender",
            1,
            5_000,
            NOW_MS,
        ).single()

        assertEquals(1, repository.withdrawConsent(grantId, NOW_MS + 1))
        assertNull(database.companionDao().outbox(OPT_IN_ID))
        assertEquals(0, repository.withdrawConsent(grantId, NOW_MS + 2))
        assertThrows(IllegalArgumentException::class.java) {
            runBlocking {
                repository.queueOutbound(
                    OBSERVATION_KIND,
                    body,
                    DEVICE_ID,
                    REMOTE_ID,
                    NOW_MS + 2,
                )
            }
        }
        Unit
    }

    @Test
    fun interventionResponseIsBoundAndAtomicallyQueued() = runBlocking {
        val intervention = fixture("intervention.json")
        assertTrue(
            repository.acceptIntervention(
                intervention,
                REMOTE_ID,
                DEVICE_ID,
                HUMAN_ID,
                NOW_MS,
            ),
        )
        assertFalse(
            repository.acceptIntervention(
                intervention,
                REMOTE_ID,
                DEVICE_ID,
                HUMAN_ID,
                NOW_MS,
            ),
        )
        assertTrue(
            repository.markInterventionPresented(
                INTERVENTION_ID,
                NOW_MS + 1,
            ),
        )

        val invalid = fixture("user-response.json")
            .replace("snooze.15m", "not-offered")
        assertThrows(IllegalArgumentException::class.java) {
            runBlocking {
                repository.recordUserResponse(
                    invalid,
                    DEVICE_ID,
                    REMOTE_ID,
                    HUMAN_ID,
                    NOW_MS + 1_000,
                )
            }
        }
        assertNull(database.companionDao().outbox(RESPONSE_ID))
        assertEquals(
            "presented",
            database.companionDao()
                .intervention(INTERVENTION_ID)?.state,
        )

        val response = fixture("user-response.json")
        assertTrue(
            repository.recordUserResponse(
                response,
                DEVICE_ID,
                REMOTE_ID,
                HUMAN_ID,
                NOW_MS + 1_000,
            ),
        )
        assertFalse(
            repository.recordUserResponse(
                response,
                DEVICE_ID,
                REMOTE_ID,
                HUMAN_ID,
                NOW_MS + 1_000,
            ),
        )
        assertEquals(
            RESPONSE_ID,
            database.companionDao()
                .intervention(INTERVENTION_ID)?.responseId,
        )
        assertEquals(1, repository.localStatus().pendingOutbox)
        assertEquals(0, repository.localStatus().openInterventions)
    }

    @Test
    fun authenticatedEncryptionRejectsAadTampering() {
        val plaintext = "sensitive companion payload".toByteArray()
        val aad = "record-binding".toByteArray()
        val encrypted = cipher.encrypt(plaintext, aad)
        assertArrayEquals(plaintext, cipher.decrypt(encrypted, aad))
        assertThrows(Exception::class.java) {
            cipher.decrypt(encrypted, "other-binding".toByteArray())
        }
    }

    private fun fixture(name: String): String =
        requireNotNull(javaClass.classLoader?.getResource(name))
            .readText()

    private fun optInObservation(grantId: String): String =
        fixture("observation-batch.json")
            .replace(OBSERVATION_ID, OPT_IN_ID)
            .replace(
                """"basis": "device-essential"""",
                """"basis": "user-opt-in"""",
            )
            .replace(
                """"grant_id": """"",
                """"grant_id": "$grantId"""",
            )
            .replace("device.network", "human.presence")
            .replace(
                Regex(
                    """"value": \{\s*"metered": false,\s*""" +
                        """"transport": "wifi"\s*\}""",
                ),
                """"value": {"state": "present"}""",
            )

    private class JvmPayloadCipher : PayloadCipher {
        private val key: SecretKey =
            KeyGenerator.getInstance("AES").apply { init(256) }.generateKey()
        private val random = SecureRandom()

        override fun encrypt(
            plaintext: ByteArray,
            aad: ByteArray,
        ): EncryptedPayload {
            val iv = ByteArray(12).also(random::nextBytes)
            val engine = Cipher.getInstance("AES/GCM/NoPadding")
            engine.init(
                Cipher.ENCRYPT_MODE,
                key,
                GCMParameterSpec(128, iv),
            )
            engine.updateAAD(aad)
            return EncryptedPayload(engine.doFinal(plaintext), iv)
        }

        override fun decrypt(
            payload: EncryptedPayload,
            aad: ByteArray,
        ): ByteArray {
            val engine = Cipher.getInstance("AES/GCM/NoPadding")
            engine.init(
                Cipher.DECRYPT_MODE,
                key,
                GCMParameterSpec(128, payload.iv),
            )
            engine.updateAAD(aad)
            return engine.doFinal(payload.ciphertext)
        }
    }

    private companion object {
        const val NOW_MS = 1_700_000_000_100L
        const val DEVICE_ID = "an1aaaaaaaaaaaaaaaaaaaa"
        const val REMOTE_ID = "an1cccccccccccccccccccc"
        const val HUMAN_ID = "hu1bbbbbbbbbbbbbbbbbbbb"
        const val OBSERVATION_KIND = "companion.observation.batch"
        const val OBSERVATION_ID = "01010101010101010101010101010101"
        const val OPT_IN_ID = "11111111111111111111111111111111"
        const val INTERVENTION_ID = "04040404040404040404040404040404"
        const val RESPONSE_ID = "05050505050505050505050505050505"
    }
}

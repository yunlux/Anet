package io.anet.companion.app.data

import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class EncryptedPayload(
    val ciphertext: ByteArray,
    val iv: ByteArray,
)

interface PayloadCipher {
    fun encrypt(plaintext: ByteArray, aad: ByteArray): EncryptedPayload
    fun decrypt(payload: EncryptedPayload, aad: ByteArray): ByteArray
}

class AndroidKeystorePayloadCipher(
    private val alias: String = "anet.companion.queue.v1",
) : PayloadCipher {
    private val keyStore = KeyStore.getInstance("AndroidKeyStore").apply {
        load(null)
    }

    override fun encrypt(
        plaintext: ByteArray,
        aad: ByteArray,
    ): EncryptedPayload {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, key())
        cipher.updateAAD(aad)
        return EncryptedPayload(
            ciphertext = cipher.doFinal(plaintext),
            iv = cipher.iv.copyOf(),
        )
    }

    override fun decrypt(
        payload: EncryptedPayload,
        aad: ByteArray,
    ): ByteArray {
        require(payload.iv.size == GCM_IV_BYTES) {
            "invalid encrypted payload IV"
        }
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(
            Cipher.DECRYPT_MODE,
            key(),
            GCMParameterSpec(128, payload.iv),
        )
        cipher.updateAAD(aad)
        return cipher.doFinal(payload.ciphertext)
    }

    private fun key(): SecretKey {
        val existing = keyStore.getKey(alias, null)
        if (existing is SecretKey) {
            return existing
        }
        val generator = KeyGenerator.getInstance(
            KeyProperties.KEY_ALGORITHM_AES,
            "AndroidKeyStore",
        )
        generator.init(
            KeyGenParameterSpec.Builder(
                alias,
                KeyProperties.PURPOSE_ENCRYPT or
                    KeyProperties.PURPOSE_DECRYPT,
            )
                .setKeySize(256)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setRandomizedEncryptionRequired(true)
                .setUserAuthenticationRequired(false)
                .build(),
        )
        return generator.generateKey()
    }

    private companion object {
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val GCM_IV_BYTES = 12
    }
}

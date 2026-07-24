package io.anet.companion

import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.put
import java.security.MessageDigest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFailsWith
import kotlin.test.assertTrue

class CompanionProtocolTest {
    private val fixtures = mapOf(
        "observation-batch.json" to CompanionProtocol.OBSERVATION_BATCH,
        "episode.json" to CompanionProtocol.EPISODE,
        "intervention.json" to CompanionProtocol.INTERVENTION,
        "user-response.json" to CompanionProtocol.USER_RESPONSE,
        "approval-request.json" to CompanionProtocol.APPROVAL_REQUEST,
        "approval-decision.json" to CompanionProtocol.APPROVAL_DECISION,
    )

    @Test
    fun allPythonFixturesValidateAndNormalizeIdempotently() {
        for ((resource, kind) in fixtures) {
            val encoded = resource(resource)
            val normalized = CompanionProtocol.parseAndValidate(kind, encoded)
            val canonical = CompanionProtocol.canonicalJson(normalized)
            val reparsed = CompanionProtocol.parseAndValidate(kind, canonical)
            assertEquals(normalized, reparsed, resource)
            assertTrue(canonical.contains("\"protocol\":\"anet.companion\""))
        }
    }

    @Test
    fun canonicalOutputMatchesPythonContractByteForByte() {
        val expected = CompanionProtocol.json.parseToJsonElement(
            resource("canonical-sha256.json"),
        ).jsonObject
        val actual = fixtures.mapValues { (resource, kind) ->
            val normalized = CompanionProtocol.parseAndValidate(
                kind,
                resource(resource),
            )
            val bytes = CompanionProtocol.canonicalJson(normalized)
                .toByteArray(Charsets.UTF_8)
            MessageDigest.getInstance("SHA-256")
                .digest(bytes)
                .joinToString("") { "%02x".format(it) }
        }
        assertEquals(
            expected.mapValues { it.value.jsonPrimitive.content },
            actual,
        )
    }

    @Test
    fun approvalFixtureHasExactCrossObjectBinding() {
        val request = validated(
            "approval-request.json",
            CompanionProtocol.APPROVAL_REQUEST,
        )
        val decision = validated(
            "approval-decision.json",
            CompanionProtocol.APPROVAL_DECISION,
        )
        assertEquals(
            decision,
            CompanionProtocol.validateApprovalDecisionBinding(
                request,
                decision,
            ),
        )
    }

    @Test
    fun unknownAndForbiddenFieldsFailClosed() {
        val batch = validated(
            "observation-batch.json",
            CompanionProtocol.OBSERVATION_BATCH,
        )
        val unknown = copy(batch, "server_override", JsonPrimitive("yes"))
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.OBSERVATION_BATCH,
                unknown,
            )
        }
        val observations = batch["observations"]!!
        val first = observations.jsonArray.first().jsonObject
        val forbiddenValue = copy(
            first["value"]!!.jsonObject,
            "raw_events",
            JsonPrimitive("secret"),
        )
        val forbiddenObservation = copy(first, "value", forbiddenValue)
        val forbidden = copy(
            batch,
            "observations",
            kotlinx.serialization.json.JsonArray(
                listOf(forbiddenObservation) + observations.jsonArray.drop(1),
            ),
        )
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.OBSERVATION_BATCH,
                forbidden,
            )
        }
    }

    @Test
    fun consentAndDuplicateObservationIdsFailClosed() {
        val batch = validated(
            "observation-batch.json",
            CompanionProtocol.OBSERVATION_BATCH,
        )
        val consent = copy(
            batch["consent"]!!.jsonObject,
            "basis",
            JsonPrimitive("user-opt-in"),
        )
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.OBSERVATION_BATCH,
                copy(batch, "consent", consent),
            )
        }
        val observations = batch["observations"]!!.jsonArray
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.OBSERVATION_BATCH,
                copy(
                    batch,
                    "observations",
                    kotlinx.serialization.json.JsonArray(
                        observations + observations.first(),
                    ),
                ),
            )
        }
    }

    @Test
    fun approvalTamperAndEndpointTransplantFailClosed() {
        val request = validated(
            "approval-request.json",
            CompanionProtocol.APPROVAL_REQUEST,
        )
        val decision = validated(
            "approval-decision.json",
            CompanionProtocol.APPROVAL_DECISION,
        )
        val action = copy(
            decision["action"]!!.jsonObject,
            "parameters_digest",
            JsonPrimitive("cd".repeat(32)),
        )
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validateApprovalDecisionBinding(
                request,
                copy(decision, "action", action),
            )
        }
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validateEndpointBinding(
                CompanionProtocol.APPROVAL_REQUEST,
                request,
                senderNodeId = "an1aaaaaaaaaaaaaaaaa",
                destinationNodeId = "an1bbbbbbbbbbbbbbbbb",
                nowMs = request["created_ms"]!!.jsonPrimitive.long,
            )
        }
    }

    @Test
    fun integerAndTextTypesAreNotCoerced() {
        val response = validated(
            "user-response.json",
            CompanionProtocol.USER_RESPONSE,
        )
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.USER_RESPONSE,
                copy(response, "created_ms", JsonPrimitive("1700000001000")),
            )
        }
        assertFailsWith<CompanionValidationException> {
            CompanionProtocol.validate(
                CompanionProtocol.USER_RESPONSE,
                copy(response, "text", JsonPrimitive(42)),
            )
        }
    }

    private fun validated(resource: String, kind: String): JsonObject =
        CompanionProtocol.parseAndValidate(kind, resource(resource))

    private fun resource(name: String): String =
        requireNotNull(javaClass.classLoader.getResource(name)) {
            "missing fixture $name"
        }.readText()

    private fun copy(
        original: JsonObject,
        key: String,
        value: kotlinx.serialization.json.JsonElement,
    ): JsonObject = buildJsonObject {
        original.forEach { (existingKey, existingValue) ->
            put(existingKey, if (existingKey == key) value else existingValue)
        }
        if (key !in original) {
            put(key, value)
        }
    }
}

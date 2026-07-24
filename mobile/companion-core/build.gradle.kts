plugins {
    `java-library`
    kotlin("jvm")
}

group = "io.anet"
version = "0.1.0"

dependencies {
    api("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
    testImplementation(kotlin("test"))
}

dependencyLocking {
    lockAllConfigurations()
}

kotlin {
    jvmToolchain(21)
    compilerOptions {
        allWarningsAsErrors.set(true)
    }
}

sourceSets {
    test {
        resources.srcDir("../../docs/examples/companion-v1")
    }
}

tasks.test {
    useJUnitPlatform()
}

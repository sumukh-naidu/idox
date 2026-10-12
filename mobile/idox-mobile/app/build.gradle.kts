plugins {
    id("com.android.application")
}

android {
    namespace = "com.idox.mobile"
    compileSdk = 35

    defaultConfig {
        // Its own package: installs next to VLM Tester and never replaces it.
        applicationId = "com.idox.mobile"
        minSdk = 28
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
        ndk { abiFilters += "arm64-v8a" }
    }

    // llama-server ships as libllama-server.so and is started as a process, so the
    // libraries must be extracted to disk (nativeLibraryDir), as VLM Tester does.
    packaging { jniLibs { useLegacyPackaging = true } }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    // Comparison test only (TextLayerTest): reading a PDF's text layer. Apache-2.0.
    implementation("com.tom-roush:pdfbox-android:2.0.27.0")
}

import java.io.File

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    id("com.chaquo.python")
}

chaquopy {
    defaultConfig {
        version = "3.11"
        pip {
            install("openai")
            install("httpx<0.28")
            install("pandas")
            install("numpy")
            install("requests")
            install("python-frontmatter")
            install("portalocker")
        }
    }
}

android {
    namespace = "com.f1993yan.openInvest"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.f1993yan.openInvest"
        minSdk = 36
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"

        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"

        ndk {
            abiFilters.addAll(setOf("arm64-v8a", "x86_64"))
        }
    }

    buildTypes {
        release {
            optimization {
                enable = false
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
    }
    buildFeatures {
        compose = true
    }
}

val projectRootDir = project.rootDir
val appProjectDir = projectDir

tasks.register("copyPythonSources") {
    val srcDir = projectRootDir.parentFile
    val destDir = File(appProjectDir, "src/main/python")
    
    inputs.files(listOf("core", "utils", "agents", "db", "jobs").map { File(srcDir, it) })
    outputs.dir(destDir)

    doLast {
        destDir.deleteRecursively()
        destDir.mkdirs()
        
        val dirsToCopy = listOf("core", "utils", "agents", "db", "jobs")
        dirsToCopy.forEach { dirName ->
            val fromDir = File(srcDir, dirName)
            if (fromDir.exists()) {
                fromDir.copyRecursively(File(destDir, dirName), overwrite = true)
            }
        }
    }
}

tasks.named("preBuild") {
    dependsOn("copyPythonSources")
}

tasks.configureEach {
    if (name.startsWith("merge") && name.endsWith("PythonSources")) {
        dependsOn("copyPythonSources")
    }
}

dependencies {
    implementation(platform(libs.androidx.compose.bom))
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.ui.graphics)
    implementation(libs.androidx.compose.ui.tooling.preview)
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.lifecycle.runtime.ktx)
    implementation(libs.okhttp)
    implementation(libs.gson)
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.compose.material:material-icons-extended")
    testImplementation(libs.junit)
    androidTestImplementation(platform(libs.androidx.compose.bom))
    androidTestImplementation(libs.androidx.compose.ui.test.junit4)
    androidTestImplementation(libs.androidx.espresso.core)
    androidTestImplementation(libs.androidx.junit)
    debugImplementation(libs.androidx.compose.ui.test.manifest)
    debugImplementation(libs.androidx.compose.ui.tooling)
}
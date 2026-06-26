import java.io.File

plugins {
    alias(libs.plugins.android.library)
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
    namespace = "com.f1993yan.openInvest.core"
    compileSdk = 36

    defaultConfig {
        minSdk = 36

        ndk {
            abiFilters.addAll(setOf("arm64-v8a", "x86_64"))
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_11
        targetCompatibility = JavaVersion.VERSION_11
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
    implementation(libs.androidx.core.ktx)
    implementation(libs.okhttp)
    implementation(libs.gson)
}

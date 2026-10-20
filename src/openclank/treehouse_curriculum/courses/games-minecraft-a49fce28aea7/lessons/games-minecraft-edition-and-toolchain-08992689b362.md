# Match the edition and toolchain

- Java Edition1.21.11 and Fabric form this independent mod route. Bedrock uses a different add-on workflow.
- Use a fresh owned folder and disposable world. Extract [Fabric example commit e92cc90](https://github.com/FabricMC/fabric-example-mod/tree/e92cc90e7fc8e130c042ea1cdc7c7bb748194d81). Keep its wrapper files and LICENSE. Remove only that fresh template’s example Java/client classes, mixin JSONs and assets/modid; the next unit supplies all replacement source/resources.
- The build launcher/daemon uses **JDK 25** because Loom 1.18.4 requires it. Compile with **JDK 21/release21**; development game/datagen and packaged game also use **Java 21**. Source compatibility cannot make a JVM21 load the JVM25 plugin.
- These exact direct coordinates are source-inspected candidates, not a tested environment lock. Record installed JDK vendor/patch and OS; verify wrapper distribution hash against official Gradle metadata. Dependency resolution, graphics and actual child JVM still need observation.
- Replace the following three files in the new fixture. Keep Gradle wrapper9.7.1.

`settings.gradle`
```groovy
pluginManagement {
    repositories {
        maven { url = 'https://maven.fabricmc.net/' }
        mavenCentral()
        gradlePluginPortal()
    }
}
rootProject.name = 'target_square'
```

`gradle.properties`
```properties
org.gradle.jvmargs=-Xmx1G
org.gradle.parallel=true
org.gradle.configuration-cache=true
minecraft_version=1.21.11
loader_version=0.19.5
loom_version=1.18.4
version=1.0.0
group=org.example.targetsquare
fabric_api_version=0.141.6+1.21.11
```

`build.gradle`
```groovy
plugins { id 'net.fabricmc.fabric-loom-remap' version "${loom_version}" }
loom {
    splitEnvironmentSourceSets()
    mods { "target_square" { sourceSet sourceSets.main; sourceSet sourceSets.client } }
}
dependencies {
    minecraft "com.mojang:minecraft:${project.minecraft_version}"
    mappings loom.officialMojangMappings()
    modImplementation "net.fabricmc:fabric-loader:${project.loader_version}"
    modImplementation "net.fabricmc.fabric-api:fabric-api:${project.fabric_api_version}"
}
processResources {
    inputs.property "version", project.version
    filesMatching("fabric.mod.json") { expand "version": project.version }
}
java {
    toolchain { languageVersion = JavaLanguageVersion.of(21) }
    withSourcesJar()
    sourceCompatibility = JavaVersion.VERSION_21
    targetCompatibility = JavaVersion.VERSION_21
}
tasks.withType(JavaCompile).configureEach { options.release = 21 }
def gameJava21 = javaToolchains.launcherFor { languageVersion = JavaLanguageVersion.of(21) }
tasks.withType(JavaExec).configureEach { javaLauncher.set(gameJava21) }
jar { from("LICENSE") { rename { "LICENSE_target_square" } } }
```

- Select installed JDK 25 for the shell's JAVA_HOME and an owned disposable GRADLE_USER_HOME. On macOS/Linux use `./gradlew`; PowerShell uses `.\gradlew.bat` with `$env:JAVA_HOME`/`$env:GRADLE_USER_HOME` set to the selected paths. Do not change global startup files.
- After adding the next unit’s files: run `--no-daemon --version`, `--no-daemon -q javaToolchains`, `--no-daemon build`, `--no-daemon dependencies --configuration runtimeClasspath`, then `--no-daemon runClient`. Inspect actual daemon25/compiler21 and development child21; `--info` can expose its launch command. Redact secrets/private paths in shared evidence. Inspect compiled class major 65 with JDK 21 javap.
- An IDE’s Gradle JVM and game launch are separately selected; this route qualifies wrapper commands first.

**Self-check:** Create the supplied candidate workspace; show launcher/daemon25, compiler/release21 and actual game Java21. Submit build/runtime versions, command results and first failed boundary if any.

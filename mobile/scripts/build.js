#!/usr/bin/env node
// Builds dist/ilo-fans.apk: a debug-signed APK you can install directly.
// Needs Node 18+, JDK 17 and the Android SDK (ANDROID_HOME or ANDROID_SDK_ROOT).
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const sh = (cmd, cwd = root) => { console.log(`\n> ${cmd}`); execSync(cmd, { cwd, stdio: "inherit" }); };

if (!process.env.ANDROID_HOME && !process.env.ANDROID_SDK_ROOT) {
  console.error("ANDROID_HOME is not set. Install the Android SDK (or Android Studio) first, " +
                "or build in the cloud with the GitHub Actions workflow - see README.");
  process.exit(1);
}

// App icon, shared with the web panel.
fs.mkdirSync(path.join(root, "resources"), { recursive: true });
fs.copyFileSync(path.join(root, "..", "web", "icons", "app-1024.png"), path.join(root, "resources", "icon-only.png"));
fs.copyFileSync(path.join(root, "..", "web", "icons", "icon-192.png"), path.join(root, "www", "icon.png"));

if (!fs.existsSync(path.join(root, "android"))) sh("npx cap add android");

// Panels on a LAN are usually plain http://; Android blocks that unless told otherwise.
const manifest = path.join(root, "android", "app", "src", "main", "AndroidManifest.xml");
let xml = fs.readFileSync(manifest, "utf8");
if (!xml.includes("usesCleartextTraffic")) {
  xml = xml.replace("<application", '<application android:usesCleartextTraffic="true"');
  fs.writeFileSync(manifest, xml);
}

sh('npx capacitor-assets generate --android --iconBackgroundColor "#0B0E13" --iconBackgroundColorDark "#0B0E13"');
sh("npx cap sync android");

const gradlew = process.platform === "win32" ? "gradlew.bat" : "./gradlew";
if (process.platform !== "win32") fs.chmodSync(path.join(root, "android", "gradlew"), 0o755);
sh(`${gradlew} assembleDebug`, path.join(root, "android"));

const apk = path.join(root, "android", "app", "build", "outputs", "apk", "debug", "app-debug.apk");
fs.mkdirSync(path.join(root, "dist"), { recursive: true });
fs.copyFileSync(apk, path.join(root, "dist", "ilo-fans.apk"));
console.log(`\nDone: ${path.join(root, "dist", "ilo-fans.apk")}`);

const { app, BrowserWindow } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const http = require("http");
const fs = require("fs");

const PORT = Number(process.env.MODUS_DESKTOP_PORT || 3000);
const HOST = process.env.MODUS_DESKTOP_HOST || "127.0.0.1";
const PROJECT_DIR = path.resolve(__dirname, "..");

let pythonProcess = null;
let mainWindow = null;

function serverIsReady() {
  return new Promise((resolve) => {
    const request = http.get(`http://${HOST}:${PORT}/api/health`, (res) => {
      res.resume();
      resolve(res.statusCode === 200);
    });
    request.setTimeout(750, () => request.destroy());
    request.on("error", () => resolve(false));
  });
}

function pythonCommand() {
  const relative = process.platform === "win32" ? ["Scripts", "python.exe"] : ["bin", "python"];
  const projectPython = path.join(PROJECT_DIR, ".venv", ...relative);
  if (fs.existsSync(projectPython)) {
    return {
      command: projectPython,
      args: ["-m", "modus", "serve", "--port", String(PORT), "--host", HOST],
    };
  }
  return {
    command: process.env.MODUS_UV || "uv",
    args: ["run", "modus", "serve", "--port", String(PORT), "--host", HOST],
  };
}

async function startPythonServer() {
  if (await serverIsReady()) {
    console.log("Server already running on port", PORT);
    return;
  }
  const launch = pythonCommand();
  await new Promise((resolve, reject) => {
    pythonProcess = spawn(launch.command, launch.args, {
      cwd: PROJECT_DIR,
      env: { ...process.env },
      stdio: ["ignore", "pipe", "pipe"],
    });

    pythonProcess.stdout.on("data", (data) => {
      console.log("[python]", data.toString().trim());
    });

    pythonProcess.stderr.on("data", (data) => {
      console.error("[python:err]", data.toString().trim());
    });

    pythonProcess.on("error", (err) => {
      console.error("Failed to start Python server:", err);
      reject(err);
    });

    pythonProcess.once("spawn", resolve);

    pythonProcess.on("exit", (code) => {
      console.log("Python server exited with code", code);
      pythonProcess = null;
    });
  });
}

function waitForServer() {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 15000;
    const check = async () => {
      if (await serverIsReady()) {
        resolve();
      } else if (pythonProcess === null) {
        reject(new Error("Python server exited before becoming ready"));
      } else if (Date.now() >= deadline) {
        reject(new Error("Server ready timeout"));
      } else {
        setTimeout(check, 300);
      }
    };
    check();
  });
}

async function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: "Modus",
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
    backgroundColor: "#0a0a0f",
    show: false,
  });

  mainWindow.loadURL(`http://${HOST}:${PORT}/`);

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
    if (pythonProcess) {
      pythonProcess.kill();
      pythonProcess = null;
    }
  });
}

app.commandLine.appendSwitch("disable-software-rasterizer");
app.commandLine.appendSwitch("log-level", "3");  // 只显示严重错误

app.on("ready", async () => {
  try {
    console.log("Starting Python server...");
    await startPythonServer();
    console.log("Waiting for server...");
    await waitForServer();
    console.log("Server ready, creating window...");
    await createWindow();
  } catch (err) {
    console.error("Startup failed:", err.message);
    app.quit();
  }
});

app.on("window-all-closed", () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  app.quit();
});

app.on("before-quit", () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
});

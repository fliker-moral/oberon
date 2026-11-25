using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("Oberon")]
[assembly: AssemblyProduct("Oberon")]
[assembly: AssemblyDescription("Рабочее пространство для анализа проектов")]
[assembly: AssemblyVersion("3.5.2.0")]
[assembly: AssemblyFileVersion("3.5.2.0")]

internal static class Program
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);

    [STAThread]
    private static int Main(string[] args)
    {
        SetCurrentProcessExplicitAppUserModelID("Oberon.Desktop");
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        string customData = Environment.GetEnvironmentVariable("OBERON_DATA_DIR");
        bool verify = Array.IndexOf(args, "--check") >= 0;
        string dataDir = String.IsNullOrWhiteSpace(customData)
            ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Oberon")
            : Path.GetFullPath(customData);
        string id;
        using (var hash = SHA256.Create())
        {
            id = BitConverter.ToString(hash.ComputeHash(System.Text.Encoding.UTF8.GetBytes(dataDir.ToUpperInvariant())));
        }
        bool first;
        using (var mutex = new Mutex(true, "Local\\Oberon_" + id, out first))
        {
            if (!first)
            {
                MessageBox.Show("Oberon уже открыт.", "Oberon", MessageBoxButtons.OK, MessageBoxIcon.Information);
                return 1;
            }
            using (var window = new OberonWindow(dataDir, verify))
            {
                Application.Run(window);
                return window.Result;
            }
        }
    }
}

internal sealed class OberonWindow : Form
{
    private readonly string dataDir;
    private readonly string logPath;
    private readonly WebView2 view;
    private readonly Label status;
    private readonly bool verify;
    private readonly System.Windows.Forms.Timer startup;
    private Process backend;
    private Uri page;
    private bool closing;
    private bool interfaceReady;
    private DateTime lastRendererReload;

    public int Result { get; private set; }

    public OberonWindow(string dataDir, bool verify)
    {
        this.dataDir = dataDir;
        logPath = Path.Combine(dataDir, "logs", "launcher.log");
        this.verify = verify;
        Result = 1;
        Text = "Oberon";
        Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        BackColor = Color.FromArgb(16, 16, 19);
        ForeColor = Color.WhiteSmoke;
        StartPosition = FormStartPosition.CenterScreen;
        ClientSize = new Size(1200, 800);
        MinimumSize = new Size(700, 500);
        if (verify) { Opacity = 0; ShowInTaskbar = false; }
        status = new Label {
            Text = "Запуск Oberon…", Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font("Segoe UI", 13), ForeColor = Color.WhiteSmoke
        };
        view = new WebView2 { Dock = DockStyle.Fill, Visible = false, DefaultBackgroundColor = BackColor };
        Controls.Add(view);
        Controls.Add(status);
        startup = new System.Windows.Forms.Timer { Interval = 22000 };
        startup.Tick += (sender, args) => ShowError("Интерфейс Oberon не загрузился за отведённое время.");
        Shown += Start;
        FormClosed += (sender, args) => {
            closing = true;
            Log("window_closed reason=" + args.CloseReason);
            startup.Dispose();
            CleanUp();
        };
    }

    private async void Start(object sender, EventArgs args)
    {
        try
        {
            startup.Start();
            Log("window_start version=" + Assembly.GetExecutingAssembly().GetName().Version);
            string runtime = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime");
            string secret;
            using (var random = RandomNumberGenerator.Create())
            {
                byte[] bytes = new byte[32];
                random.GetBytes(bytes);
                secret = Convert.ToBase64String(bytes);
            }
            int port = await StartBackend(runtime, secret);
            if (closing) return;

            page = new Uri("http://127.0.0.1:" + port + "/");
            var environment = await CoreWebView2Environment.CreateAsync(
                userDataFolder: Path.Combine(dataDir, "WebView2"));
            await view.EnsureCoreWebView2Async(environment);
            if (closing) return;
            var browser = view.CoreWebView2;
            browser.Settings.AreDevToolsEnabled = false;
            browser.Settings.AreHostObjectsAllowed = false;
            browser.Settings.AreDefaultScriptDialogsEnabled = true;
            browser.Settings.IsStatusBarEnabled = false;
            browser.NavigationStarting += (s, e) => e.Cancel = !IsAppUri(e.Uri);
            browser.NewWindowRequested += (s, e) => e.Handled = true;
            browser.PermissionRequested += (s, e) => e.State = CoreWebView2PermissionState.Deny;
            browser.AddWebResourceRequestedFilter(page.AbsoluteUri + "*", CoreWebView2WebResourceContext.All);
            browser.WebResourceRequested += (s, e) =>
            {
                if (IsAppUri(e.Request.Uri)) e.Request.Headers.SetHeader("X-Oberon-Desktop", secret);
            };
            browser.WebMessageReceived += ReceiveMessage;
            browser.ProcessFailed += BrowserProcessFailed;
            browser.NavigationCompleted += (s, e) =>
            {
                if (e.IsSuccess || closing || e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
                Log("navigation_failed status=" + e.WebErrorStatus);
                if (interfaceReady) ShowRuntimeError("Не удалось загрузить интерфейс Oberon.");
                else ShowError("Не удалось открыть интерфейс Oberon.");
            };
            status.Visible = false;
            view.Visible = true;
            browser.Navigate(page.AbsoluteUri);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            if (!closing) ShowError("Для запуска требуется Microsoft Edge WebView2 Runtime.");
        }
        catch (Exception exception)
        {
            Log("startup_failed type=" + exception.GetType().Name);
            if (!closing) ShowError("Не удалось запустить Oberon.");
        }
    }

    private async Task<int> StartBackend(string runtime, string secret)
    {
        var start = new ProcessStartInfo {
            FileName = Path.Combine(runtime, "python.exe"),
            Arguments = "-B -u -m oberon.web --desktop",
            WorkingDirectory = runtime,
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
            StandardOutputEncoding = System.Text.Encoding.UTF8, StandardErrorEncoding = System.Text.Encoding.UTF8
        };
        start.EnvironmentVariables["OBERON_DATA_DIR"] = dataDir;
        start.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        backend = Process.Start(start);
        backend.Exited += BackendExited;
        backend.EnableRaisingEvents = true;
        backend.ErrorDataReceived += (sender, args) => {};
        backend.BeginErrorReadLine();
        backend.StandardInput.WriteLine(secret);
        backend.StandardInput.Flush();
        var ready = backend.StandardOutput.ReadLineAsync();
        if (await Task.WhenAny(ready, Task.Delay(12000)) != ready)
            throw new InvalidOperationException("Служба приложения не ответила за 12 секунд.");
        string line = await ready;
        int port;
        if (line == null || !Int32.TryParse(line, out port) || port < 1 || port > 65535)
            throw new InvalidOperationException("Служба приложения завершила запуск с ошибкой.");
        return port;
    }

    private void BackendExited(object sender, EventArgs args)
    {
        var process = (Process)sender;
        try
        {
            int exitCode = process.ExitCode;
            BeginInvoke(new Action(() =>
            {
                if (closing || backend != process) return;
                Log("backend_exited exit_code=" + exitCode);
                if (interfaceReady) ShowRuntimeError("Служба Oberon завершилась во время работы.");
            }));
        }
        catch (InvalidOperationException) {}
    }

    private void BrowserProcessFailed(object sender, CoreWebView2ProcessFailedEventArgs args)
    {
        if (closing) return;
        var kind = args.ProcessFailedKind;
        Log("webview_failed kind=" + kind + " reason=" + args.Reason + " exit_code=" + args.ExitCode);
        if (kind == CoreWebView2ProcessFailedKind.BrowserProcessExited)
        {
            if (interfaceReady) ShowRuntimeError("Компонент WebView2 завершился.");
            else ShowError("Компонент WebView2 завершился при запуске Oberon.");
        }
        if (kind == CoreWebView2ProcessFailedKind.RenderProcessExited)
        {
            if (DateTime.UtcNow - lastRendererReload < TimeSpan.FromSeconds(20))
            {
                ShowRuntimeError("Интерфейс Oberon прервался повторно.");
                return;
            }
            lastRendererReload = DateTime.UtcNow;
            Log("renderer_reload");
            // Leave the Python analysis worker running and reconnect to the existing job.
            view.CoreWebView2.Reload();
        }
    }

    private void Log(string message)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(logPath));
            if (File.Exists(logPath) && new FileInfo(logPath).Length > 250000)
            {
                File.Copy(logPath, logPath + ".1", true);
                File.WriteAllText(logPath, "");
            }
            File.AppendAllText(logPath, DateTimeOffset.Now.ToString("o") + " " + message + Environment.NewLine);
        }
        catch (IOException) {}
        catch (UnauthorizedAccessException) {}
    }

    private bool IsAppUri(string url)
    {
        Uri target;
        return page != null && Uri.TryCreate(url, UriKind.Absolute, out target) &&
            target.Scheme == page.Scheme && target.Host == page.Host && target.Port == page.Port;
    }

    private void ReceiveMessage(object sender, CoreWebView2WebMessageReceivedEventArgs args)
    {
        if (!IsAppUri(args.Source)) return;
        string message = args.TryGetWebMessageAsString();
        if (message == "interface-ready")
        {
            startup.Stop();
            interfaceReady = true;
            status.Visible = false;
            view.Visible = true;
            Result = 0;
            if (verify) Close();
            return;
        }
        if (message == "interface-error")
        {
            Log("interface_error");
            return;
        }
        if (message == "choose-archive")
        {
            using (var picker = new OpenFileDialog {
                Title = "Выберите ZIP-архив проекта", Filter = "Архив проекта (*.zip)|*.zip", CheckFileExists = true
            })
            {
                if (picker.ShowDialog(this) == DialogResult.OK)
                    view.CoreWebView2.PostWebMessageAsString("archive:" + picker.FileName);
            }
            return;
        }
        if (message != "choose-repository") return;
        using (var picker = new FolderBrowserDialog { Description = "Выберите папку локального Git-репозитория", ShowNewFolderButton = false })
        {
            string path = picker.ShowDialog(this) == DialogResult.OK ? picker.SelectedPath : "";
            view.CoreWebView2.PostWebMessageAsString(path);
        }
    }

    private void ShowError(string message)
    {
        startup.Stop();
        Result = 1;
        Log("startup_error");
        CleanUp();
        if (!verify) MessageBox.Show(this, message + "\r\n\r\nЖурнал: " + logPath, "Oberon", MessageBoxButtons.OK, MessageBoxIcon.Error);
        Close();
    }

    private void ShowRuntimeError(string message)
    {
        startup.Stop();
        Result = 1;
        view.Visible = false;
        status.Text = message + "\r\nОткройте Oberon заново.\r\n\r\nЖурнал: " + logPath;
        status.Visible = true;
        status.BringToFront();
        if (verify) Close();
    }

    private void CleanUp()
    {
        if (backend == null) return;
        try
        {
            backend.StandardInput.Close();
            if (!backend.WaitForExit(1000))
            {
                Log("backend_stop_timeout");
                backend.Kill();
            }
        }
        catch (InvalidOperationException) {}
        finally { backend.Dispose(); backend = null; }
    }
}

// A native .NET console entry point for the signed python.org runtime shipped beside it.
// The archive must be extracted in full. Configuration and player data live beside this exe.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;

[assembly: AssemblyTitle("GRFS Server")]
[assembly: AssemblyProduct("GRFS Server")]
[assembly: AssemblyVersion("1.0.0.0")]

namespace GrfsServer
{
    internal static class Program
    {
        private static int Main(string[] args)
        {
            Console.OutputEncoding = new UTF8Encoding(false);
            try
            {
                string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
                string python = Path.Combine(root, "python", "python.exe");
                if (!File.Exists(python) || !File.Exists(Path.Combine(root, "app", "grfs", "__main__.py")))
                    throw new FileNotFoundException("Extract the whole GRFS-Windows-x64.zip before starting the server.");
                StringBuilder arguments = new StringBuilder("-X utf8 -m grfs");
                foreach (string argument in args)
                    arguments.Append(" ").Append(Quote(argument));
                ProcessStartInfo start = new ProcessStartInfo(
                    python, arguments.ToString());
                start.UseShellExecute = false;
                start.WorkingDirectory = root;
                start.EnvironmentVariables["GRFS_RUNTIME_DIR"] = root;
                // Both processes receive Ctrl+C. Let Python save its stores and stop first.
                Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
                using (Process server = Process.Start(start))
                {
                    server.WaitForExit();
                    return server.ExitCode;
                }
            }
            catch (Exception error)
            {
                Console.Error.WriteLine("Cannot start GRFS server: " + error.Message);
                return 1;
            }
        }

        private static string Quote(string value)
        {
            StringBuilder result = new StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\')
                {
                    slashes++;
                    continue;
                }
                result.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
                slashes = 0;
                result.Append(c);
            }
            result.Append('\\', slashes * 2).Append('"');
            return result.ToString();
        }
    }
}

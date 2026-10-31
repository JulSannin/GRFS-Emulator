// GRFS Connect: what a player runs on another machine to play Ghost Recon Future Soldier on a
// GRFS server. tools/make_connect.py builds it into dist\GRFS-Connect.exe.
//
// It does what the game needs from that machine, in this order:
//
//   1. Asks the server for its configuration the way the game does, then sends the NAT echo
//      question to both PRUDP ports and the NAT type detector's first question to the port the
//      configuration names. All of it before anything is changed: a server that is not up must
//      not leave a redirect behind.
//   2. Points onlineconfigservice.ubi.com at the server in the hosts file. It is the one name the
//      game resolves; everything after it is addresses the server hands out itself.
//   3. Lets the other players in: inbound UDP 2346-2347, and the firewall's block rules for the
//      game switched off when someone once pressed Cancel on its prompt.
//   4. Starts the game - with _CLUSTER_NETWORK_NAME_ set when it has to be. The game binds UDP
//      2346 to the first address its machine name resolves to, and when that is a VPN adapter's,
//      the server is out of its reach and joining a session hangs. The variable is Windows' own
//      way to change the machine name for one process; set to the address that reaches the
//      server, it is the address the game binds. Nothing in the game or its folder is touched.
//
// It runs as the user and asks for elevation only for 2 and 3, by starting itself again with
// --elevated: a game started from an elevated process would run elevated as well, which is not
// how it is normally run.
//
// The compiler is the one in .NET Framework 4, which every Windows 10 and 11 has - so C# 5.

using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

// What Windows shows for it: in Task Manager and in its prompt for administrator rights.
[assembly: AssemblyTitle("GRFS Connect")]
[assembly: AssemblyProduct("GRFS Connect")]
[assembly: AssemblyDescription("Connects this computer to a GRFS server for Ghost Recon Future Soldier")]
[assembly: AssemblyVersion("1.0.0.0")]

namespace GrfsConnect
{
    internal static class Program
    {
        private const string Usage =
            "GRFS-Connect.exe [АДРЕС]          подключиться к серверу и запустить игру\n" +
            "GRFS-Connect.exe --check [АДРЕС]  только проверить, ничего не меняя\n" +
            "GRFS-Connect.exe --off            отключиться: убрать строку из hosts и правило брандмауэра";

        private static bool interactive;

        private static int Main(string[] args)
        {
            Report.SetUp();
            try
            {
                return Run(args);
            }
            finally
            {
                Report.TearDown();
            }
        }

        private static int Run(string[] args)
        {
            Options o;
            try
            {
                o = Options.Parse(args);
            }
            catch (ArgumentException e)
            {
                Console.WriteLine(e.Message);
                Console.WriteLine(Usage);
                return 2;
            }
            if (o.Help)
            {
                Console.WriteLine(Usage);
                return 0;
            }
            if (o.ResultFile != null)
                return Elevation.Serve(o);
            if (o.HostsFile != null)
                return EditHostsFile(o);

            interactive = o.Interactive;
            int code;
            try
            {
                code = o.Off ? Disconnect() : Connect(o);
            }
            catch (Exception e)
            {
                Report.Bad("Непредвиденная ошибка: " + e.Message);
                code = 1;
            }
            if (interactive)
            {
                Console.WriteLine();
                Console.Write("Enter - закрыть окно.");
                Console.ReadLine();
            }
            return code;
        }

        private static int Connect(Options o)
        {
            Console.WriteLine("GRFS Connect - подключение к серверу Ghost Recon Future Soldier");
            string current = Hosts.CurrentAddress(Hosts.SystemPath);
            Report.Note(current == null
                ? "Сейчас в hosts нет строки для " + Hosts.Name + "."
                : "Сейчас hosts ведёт " + Hosts.Name + " на " + current + ".");

            string text = o.Address;
            if (text == null)
            {
                string suggestion = Baked.Server.Length > 0 ? Baked.Server : current;
                if (!interactive)
                {
                    if (suggestion == null)
                    {
                        Report.Bad("Адрес сервера не задан: например,  GRFS-Connect.exe --check 192.168.1.8");
                        return 2;
                    }
                    text = suggestion;
                }
                else
                {
                    text = AskAddress(suggestion);
                    if (text == null)
                        return 1;
                    if (text == "-")
                        return Disconnect();
                }
            }
            IPAddress address = Resolve(text);
            if (address == null)
                return 1;

            ServerView server = ServerView.Look(address, o.HttpPort, o.SecurePort);
            if (!server.ConfigOk)
            {
                if (!o.Check)
                    Report.Note("Ничего не изменено.");
                return 1;
            }
            LocalView local = LocalView.Look(server);
            Needs needs = Needs.Look(address, current);
            if (o.Check)
            {
                Console.WriteLine();
                Console.WriteLine(Report.Failures == 0 ? "Проверка пройдена." : "Есть проблемы: " + Report.Failures + ".");
                return Report.Failures == 0 ? 0 : 1;
            }

            Report.Section("Подключение");
            bool unblock = needs.Blocks.Count > 0 && Ask("Отключить запрещающие правила брандмауэра для игры?");
            if (!needs.HostsChange && !needs.RuleMissing && !unblock)
                Report.Ok("Всё уже на месте, менять ничего не нужно.");
            else
                Elevation.Apply("connect " + address + (unblock ? " --unblock" : ""));
            bool reached = CheckName(address);

            if (interactive && reached)
                Launch(local, needs.HostsChange);
            else if (interactive)
                Report.Note("Игру запускать рано: сначала hosts должен вести на сервер.");
            return Report.Failures == 0 ? 0 : 1;
        }

        private static int Disconnect()
        {
            Console.WriteLine("GRFS Connect - отключение от сервера");
            Report.Section("Отключение");
            bool hosts = Hosts.Changes(Hosts.SystemPath, null);
            bool rule;
            try
            {
                rule = Firewall.HasRule();
            }
            catch (Exception)
            {
                rule = true;
            }
            if (!hosts && !rule)
                Report.Ok("Отключать нечего: ни строки GRFS Connect в hosts, ни его правила в брандмауэре.");
            else
                Elevation.Apply("disconnect");
            string left = Hosts.CurrentAddress(Hosts.SystemPath);
            if (left != null)
                Report.Warn("В hosts осталась строка " + left + " " + Hosts.Name + ": её добавил не GRFS Connect.\n" +
                            "Если она не нужна, убери её вручную.");
            return Report.Failures == 0 ? 0 : 1;
        }

        /// <summary>--hosts-file: one step on a file of the caller's, for the tests.</summary>
        private static int EditHostsFile(Options o)
        {
            List<string> said = new List<string>();
            bool ok = o.Action == "connect"
                ? Steps.PointHosts(o.HostsFile, o.Address, said)
                : Steps.UnpointHosts(o.HostsFile, said);
            foreach (string line in said)
                Report.Relay(line);
            return ok ? 0 : 1;
        }

        /// <summary>The name resolved the way the game resolves it, after the change.</summary>
        private static bool CheckName(IPAddress address)
        {
            try
            {
                IPAddress[] found = Dns.GetHostAddresses(Hosts.Name);
                if (found.Contains(address))
                {
                    Report.Ok(Hosts.Name + " ведёт на " + address + " - игра пойдёт на сервер.");
                    return true;
                }
                Report.Bad(Hosts.Name + " ведёт на " + string.Join(", ", found.Select(a => a.ToString())) +
                           ", а не на " + address + ".\nЗапись в hosts что-то перекрывает: VPN, прокси или антивирус.");
            }
            catch (SocketException e)
            {
                Report.Bad(Hosts.Name + " не находится: " + e.Message);
            }
            return false;
        }

        /// <summary>`hostsChanged`: a game already running asked for its configuration before,
        /// at its own start, and still has the old one.</summary>
        private static void Launch(LocalView local, bool hostsChanged)
        {
            Report.Section("Игра");
            IPAddress pin = local.NeedsAddress ? local.Route : null;
            IPAddress expected = pin ?? local.First;
            if (Game.Running())
            {
                if (hostsChanged || pin != null)
                    Report.Warn("Игра уже запущена. Адрес сервера она узнаёт при запуске" +
                                (pin != null ? ", тогда же открывает UDP 2346" : "") +
                                ",\nпоэтому её надо закрыть и запустить снова" + (pin != null ? " - отсюда." : "."));
                else
                    Report.Note("Игра уже запущена.");
                Game.Watch(expected, pin != null, false);
                return;
            }
            string question = pin != null
                ? "Запустить игру сейчас, с адресом " + pin + "?"
                : "Запустить игру сейчас? (можно и как обычно, из Steam)";
            if (!Ask(question))
            {
                if (pin != null)
                    Report.Warn("Запускай игру этим exe: из Steam она снова откроет UDP 2346 на " + local.First + ".");
                return;
            }
            string folder = Game.Find() ?? AskFolder();
            if (folder == null)
            {
                Report.Bad("Без папки с игрой запустить её не получится.");
                return;
            }
            if (Elevation.IsAdmin())
                Report.Warn("Этот exe запущен от имени администратора, и игра получит те же права, а обычно их у неё\n" +
                            "нет. Если с ней что-то будет не так, запусти exe обычным двойным щелчком.");
            try
            {
                Game.Start(folder, pin);
            }
            catch (Exception e)
            {
                Report.Bad("Игра не запустилась: " + e.Message);
                return;
            }
            Report.Ok("Запущено: " + Path.Combine(folder, Game.Launcher) + (pin != null ? ", адрес " + pin : "") + ".");
            Report.Note("Если брандмауэр спросит про игру, нажми «Разрешить доступ», а не «Отмена»: «Отмена»\n" +
                        "запрещает ей входящие соединения.");
            Game.Watch(expected, pin != null, true);
        }

        // ----- questions ----------------------------------------------------------------------
        private static string AskAddress(string suggestion)
        {
            Console.WriteLine();
            Console.WriteLine("Адрес компьютера с сервером" +
                              (suggestion != null ? " (Enter - " + suggestion + ")" : "") +
                              "; «-» вместо адреса - отключиться:");
            while (true)
            {
                Console.Write("> ");
                string line = Console.ReadLine();
                if (line == null)
                    return suggestion;
                line = line.Trim();
                if (line.Length > 0)
                    return line;
                if (suggestion != null)
                    return suggestion;
            }
        }

        /// <summary>Enter or Esc, read as keys: a typed "д" or "н" depends on the keyboard
        /// layout, and the wrong one would say yes.</summary>
        private static bool Ask(string question)
        {
            if (!interactive)
                return false;
            Console.Write(question + " [Enter - да, Esc - нет] ");
            try
            {
                while (true)
                {
                    ConsoleKeyInfo key = Console.ReadKey(true);
                    if (key.Key == ConsoleKey.Enter)
                    {
                        Console.WriteLine("да");
                        return true;
                    }
                    if (key.Key == ConsoleKey.Escape)
                    {
                        Console.WriteLine("нет");
                        return false;
                    }
                }
            }
            catch (InvalidOperationException)
            {
                Console.WriteLine();
                return false;
            }
        }

        private static string AskFolder()
        {
            Console.WriteLine("Игры нет в библиотеках Steam. Перетащи сюда «" + Game.Launcher +
                              "» или вставь путь к папке игры и нажми Enter (пустая строка - отмена):");
            while (true)
            {
                Console.Write("> ");
                string line = Console.ReadLine();
                if (line == null)
                    return null;
                line = line.Trim().Trim('"');
                if (line.Length == 0)
                    return null;
                if (File.Exists(line))
                    line = Path.GetDirectoryName(line);
                if (File.Exists(Path.Combine(line, Game.Launcher)))
                    return line;
                Console.WriteLine("Там нет «" + Game.Launcher + "».");
            }
        }

        /// <summary>An IPv4 address as typed, or a name looked up in DNS: the hosts file takes
        /// only addresses.</summary>
        private static IPAddress Resolve(string text)
        {
            IPAddress ip;
            if (IPAddress.TryParse(text, out ip) && ip.AddressFamily == AddressFamily.InterNetwork &&
                ip.ToString() == text)
                return ip;
            try
            {
                foreach (IPAddress found in Dns.GetHostAddresses(text))
                {
                    if (found.AddressFamily != AddressFamily.InterNetwork)
                        continue;
                    Report.Note(text + " - это " + found + ".");
                    return found;
                }
            }
            catch (SocketException)
            {
            }
            catch (ArgumentException)
            {
            }
            Report.Bad("«" + text + "» - не адрес IPv4 и не имя, которое находится.");
            return null;
        }
    }

    /// <summary>The command line. Apart from the address it is for the program itself (the
    /// elevated half) and for the tests.</summary>
    internal sealed class Options
    {
        public string Address;
        public bool Check, Off, Help, Unblock, Interactive;
        public string ResultFile;   // --elevated FILE: the elevated half writes what it did there
        public string HostsFile;    // --hosts-file FILE: only the hosts step, on that file
        public string Action;       // connect or disconnect, after one of those two
        public int HttpPort = 80;
        public int SecurePort;      // 0: one above the authentication port, where the game has it

        public static Options Parse(string[] args)
        {
            Options o = new Options();
            List<string> rest = new List<string>();
            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "--check": o.Check = true; break;
                    case "--off": o.Off = true; break;
                    case "--unblock": o.Unblock = true; break;
                    case "--help": case "-h": case "/?": o.Help = true; break;
                    case "--elevated": o.ResultFile = Next(args, ref i); break;
                    case "--hosts-file": o.HostsFile = Next(args, ref i); break;
                    case "--http-port": o.HttpPort = Port(Next(args, ref i)); break;
                    case "--secure-port": o.SecurePort = Port(Next(args, ref i)); break;
                    default:
                        if (args[i].StartsWith("--"))
                            throw new ArgumentException("Неизвестный ключ: " + args[i]);
                        rest.Add(args[i]);
                        break;
                }
            }
            if (o.ResultFile != null || o.HostsFile != null)
            {
                o.Action = rest.Count > 0 ? rest[0] : null;
                if (o.Action == "connect" && rest.Count == 2 && IsAddress(rest[1]))
                    o.Address = rest[1];
                else if (!(o.Action == "disconnect" && rest.Count == 1))
                    throw new ArgumentException("Нужно  connect АДРЕС  или  disconnect.");
            }
            else if (rest.Count > 1)
                throw new ArgumentException("Лишнее в командной строке: " + string.Join(" ", rest.Skip(1)));
            else if (rest.Count == 1)
                o.Address = rest[0];
            o.Interactive = !o.Check && o.ResultFile == null && o.HostsFile == null &&
                            !Console.IsInputRedirected;
            return o;
        }

        private static string Next(string[] args, ref int i)
        {
            if (i + 1 >= args.Length)
                throw new ArgumentException("После " + args[i] + " нужно значение.");
            return args[++i];
        }

        private static int Port(string text)
        {
            int port;
            if (!int.TryParse(text, out port) || port < 1 || port > 65535)
                throw new ArgumentException("Не порт: " + text);
            return port;
        }

        private static bool IsAddress(string text)
        {
            IPAddress ip;
            return IPAddress.TryParse(text, out ip) && ip.AddressFamily == AddressFamily.InterNetwork &&
                   ip.ToString() == text;
        }
    }

    /// <summary>Lines on the screen, each with a mark: + done or fine, ! broken, ? worth
    /// knowing. The broken ones are counted for the exit code.</summary>
    internal static class Report
    {
        public static int Failures;
        private static Encoding consoleEncoding;

        public static void SetUp()
        {
            if (Console.IsOutputRedirected)
            {
                // Into a pipe or a file: UTF-8, whatever the console's code page is.
                StreamWriter writer = new StreamWriter(Console.OpenStandardOutput(), new UTF8Encoding(false));
                writer.AutoFlush = true;
                Console.SetOut(writer);
                return;
            }
            try
            {
                consoleEncoding = Console.OutputEncoding;
                Console.OutputEncoding = new UTF8Encoding(false);
                Console.Title = "GRFS Connect";
            }
            catch (IOException)
            {
            }
        }

        /// <summary>The console's code page back: started from a cmd window, it is that
        /// window's, and what runs in it next expects its own.</summary>
        public static void TearDown()
        {
            if (consoleEncoding == null)
                return;
            try
            {
                Console.OutputEncoding = consoleEncoding;
            }
            catch (IOException)
            {
            }
        }

        public static void Section(string title)
        {
            Console.WriteLine();
            ConsoleColor was = Console.ForegroundColor;
            Console.ForegroundColor = ConsoleColor.White;
            Console.WriteLine(title);
            Console.ForegroundColor = was;
        }

        public static void Ok(string text) { Line('+', ConsoleColor.Green, text); }
        public static void Warn(string text) { Line('?', ConsoleColor.Yellow, text); }
        public static void Note(string text) { Line(' ', ConsoleColor.Gray, text); }

        public static void Bad(string text)
        {
            Failures++;
            Line('!', ConsoleColor.Red, text);
        }

        /// <summary>A line the elevated half wrote: its first character is the mark.</summary>
        public static void Relay(string line)
        {
            if (line.Length == 0)
                return;
            string text = line.Substring(1);
            switch (line[0])
            {
                case '+': Ok(text); break;
                case '!': Bad(text); break;
                case '?': Warn(text); break;
                default: Note(text); break;
            }
        }

        private static void Line(char mark, ConsoleColor colour, string text)
        {
            string[] lines = text.Split('\n');
            ConsoleColor was = Console.ForegroundColor;
            Console.ForegroundColor = colour;
            Console.Write(" " + mark + " ");
            Console.ForegroundColor = was;
            Console.WriteLine(lines[0]);
            for (int i = 1; i < lines.Length; i++)
                Console.WriteLine("   " + lines[i]);
        }
    }

    // ===== what the server says =================================================================
    internal sealed class ServerView
    {
        public IPAddress Address;      // what this machine connects to
        public IPAddress Advertised;   // what SandboxUrl names: where the game will go
        public int AuthPort, SecurePort;
        public bool ConfigOk, AuthOk, SecureOk;

        public static ServerView Look(IPAddress address, int httpPort, int securePort)
        {
            ServerView v = new ServerView();
            v.Address = address;
            Report.Section("Сервер " + address);
            string body, error;
            int answered = Http.GetConfig(address, httpPort, out body, out error);
            if (answered < 0)
            {
                Report.Bad("Сервер не отвечает на " + address + ":" + httpPort + " (TCP): " + error + "\n" +
                           "На компьютере с сервером он должен быть запущен (python -m grfs), а брандмауэр\n" +
                           "должен пускать python.exe из этой сети: TCP 80 и UDP 21030-21033.\n" +
                           "Если здесь включён VPN, адрес сервера должен идти мимо туннеля (в AmneziaVPN -\n" +
                           "раздельное туннелирование, в исключения " + Subnet(address) + ").");
                return v;
            }
            Dictionary<string, List<string>> config = answered > 0 ? Http.ParseConfig(body) : null;
            List<string> sandbox;
            Match url = null;
            if (config != null && config.TryGetValue("SandboxUrl", out sandbox) && sandbox.Count > 0)
                url = Regex.Match(sandbox[0], @"address=([^;]+);port=(\d+)");
            IPAddress advertised = null;
            if (url == null || !url.Success || !IPAddress.TryParse(url.Groups[1].Value, out advertised))
            {
                Report.Bad("На " + address + ":" + httpPort + " отвечает что-то, но не сервер GRFS" +
                           (answered == 0 ? " (" + error + ")" : ": в ответе нет SandboxUrl с адресом") + ".");
                return v;
            }
            v.Advertised = advertised;
            v.AuthPort = int.Parse(url.Groups[2].Value);
            v.SecurePort = securePort > 0 ? securePort : v.AuthPort + 1;
            if (IPAddress.IsLoopback(advertised) && !IPAddress.IsLoopback(address))
            {
                Report.Bad("Сервер называет себя " + advertised + ", а для этого компьютера это он сам.\n" +
                           "На сервере задан advertise = " + advertised + ": его нужно убрать или поставить " + address + ".");
                return v;
            }
            v.ConfigOk = true;
            Report.Ok("Служба конфигурации отвечает: игра пойдёт на " + advertised + ":" + v.AuthPort + ".");
            if (!advertised.Equals(address))
                Report.Warn("Сервер называет себя " + advertised + ", а не " + address + ", поэтому дальше проверяется " +
                            advertised + ": туда пойдёт игра.\nЕсли отсюда до " + advertised +
                            " не достать, серверу нужен ключ  --advertise " + address + ".");

            bool hinted = false;
            string seen, why;
            v.AuthOk = Probe.NatEcho(null, new IPEndPoint(advertised, v.AuthPort), out seen, out why);
            if (v.AuthOk)
                Report.Ok("UDP " + v.AuthPort + " (вход) отвечает.");
            else
                Report.Bad("UDP " + v.AuthPort + " (вход) не отвечает: " + why + "." + UdpHint(ref hinted));
            v.SecureOk = Probe.NatEcho(null, new IPEndPoint(advertised, v.SecurePort), out seen, out why);
            if (v.SecureOk)
                Report.Ok("UDP " + v.SecurePort + " (защищённое соединение) отвечает; сервер видит этот компьютер как " +
                          seen + ".");
            else
                Report.Bad("UDP " + v.SecurePort + " (защищённое соединение) не отвечает: " + why + "." +
                           UdpHint(ref hinted));

            List<string> detect;
            IPEndPoint punch = null;
            if (config.TryGetValue("punch_DetectUrls", out detect) && detect.Count > 0)
                punch = HostPort(detect[0]);
            if (punch == null)
                Report.Warn("Сервер не прислал адрес определителя типа NAT (punch_DetectUrls): тип NAT в игре\n" +
                            "останется неизвестным.");
            else if (Probe.Punch(punch, out seen, out why))
                Report.Ok("Определитель типа NAT отвечает (UDP " + punch.Port + ").");
            else
                Report.Bad("Определитель типа NAT (UDP " + punch.Port + ") не отвечает: " + why + ".\n" +
                           "Без него тип NAT в игре неизвестен, и войти в сессию она может не дать." + UdpHint(ref hinted));
            return v;
        }

        /// <summary>The same advice once, under the first UDP port that failed.</summary>
        private static string UdpHint(ref bool hinted)
        {
            if (hinted)
                return "";
            hinted = true;
            return "\nTCP проходит, а UDP нет: на компьютере с сервером брандмауэр не пускает UDP для python.exe.\n" +
                   "Разреши python.exe в «Брандмауэр Защитника Windows - Разрешение взаимодействия с приложением»,\n" +
                   "для частных и общественных сетей.";
        }

        public static string Subnet(IPAddress address)
        {
            byte[] b = address.GetAddressBytes();
            return b[0] + "." + b[1] + "." + b[2] + ".0/24";
        }

        private static IPEndPoint HostPort(string text)
        {
            int colon = text.LastIndexOf(':');
            IPAddress ip;
            int port;
            if (colon < 0 || !IPAddress.TryParse(text.Substring(0, colon), out ip) ||
                !int.TryParse(text.Substring(colon + 1), out port))
                return null;
            return new IPEndPoint(ip, port);
        }
    }

    internal static class Http
    {
        private const string ConfigPath =
            "/OnlineConfigService.svc/GetOnlineConfig?onlineConfigID=23ad683803a0457cabce83f905811dbc&target=client";

        /// <summary>The configuration, asked for the way the game asks. The Host header names
        /// Ubisoft's service while the connection goes to `address`, so this works before the
        /// hosts file says anything. 1 with the body, 0 when something other than 200 came back,
        /// -1 when nothing did.</summary>
        public static int GetConfig(IPAddress address, int port, out string body, out string error)
        {
            body = null;
            error = null;
            try
            {
                using (TcpClient tcp = new TcpClient(AddressFamily.InterNetwork))
                {
                    IAsyncResult pending = tcp.BeginConnect(address, port, null, null);
                    if (!pending.AsyncWaitHandle.WaitOne(5000))
                    {
                        error = "соединения нет за 5 с";
                        return -1;
                    }
                    tcp.EndConnect(pending);
                    tcp.ReceiveTimeout = 5000;
                    tcp.SendTimeout = 5000;
                    NetworkStream stream = tcp.GetStream();
                    byte[] request = Encoding.ASCII.GetBytes("GET " + ConfigPath + " HTTP/1.1\r\nHost: " + Hosts.Name +
                                                             "\r\nAccept: */*\r\nConnection: close\r\n\r\n");
                    stream.Write(request, 0, request.Length);
                    MemoryStream reply = new MemoryStream();
                    byte[] buffer = new byte[4096];
                    try
                    {
                        int n;
                        while ((n = stream.Read(buffer, 0, buffer.Length)) > 0)
                            reply.Write(buffer, 0, n);
                    }
                    catch (IOException)
                    {
                        // Closed or gone quiet: what came is what there is.
                    }
                    string text = Encoding.UTF8.GetString(reply.ToArray());
                    if (text.Length == 0)
                    {
                        error = "соединение есть, но ответа нет";
                        return -1;
                    }
                    int end = text.IndexOf("\r\n", StringComparison.Ordinal);
                    string status = end >= 0 ? text.Substring(0, end) : text;
                    if (!Regex.IsMatch(status, @"^HTTP/1\.[01] 200"))
                    {
                        error = "ответ «" + (status.Length > 60 ? status.Substring(0, 60) : status) + "»";
                        return 0;
                    }
                    int split = text.IndexOf("\r\n\r\n", StringComparison.Ordinal);
                    body = split >= 0 ? text.Substring(split + 4) : "";
                    return 1;
                }
            }
            catch (SocketException e)
            {
                error = e.Message;
            }
            catch (IOException e)
            {
                error = e.Message;
            }
            return -1;
        }

        /// <summary>The JSON list of {Name, Values} as name -> values, or null.</summary>
        public static Dictionary<string, List<string>> ParseConfig(string body)
        {
            try
            {
                object[] rows = new JavaScriptSerializer().DeserializeObject(body) as object[];
                if (rows == null)
                    return null;
                Dictionary<string, List<string>> config = new Dictionary<string, List<string>>();
                foreach (object row in rows)
                {
                    IDictionary<string, object> pair = row as IDictionary<string, object>;
                    if (pair == null || !pair.ContainsKey("Name") || !pair.ContainsKey("Values"))
                        continue;
                    List<string> values = new List<string>();
                    IEnumerable list = pair["Values"] as object[];
                    if (list != null)
                        foreach (object value in list)
                            if (value != null)
                                values.Add(value.ToString());
                    config[Convert.ToString(pair["Name"])] = values;
                }
                return config;
            }
            catch (ArgumentException)
            {
                return null;
            }
            catch (InvalidOperationException)
            {
                return null;
            }
        }
    }

    /// <summary>The two questions a game asks the server over UDP before it can play.</summary>
    internal static class Probe
    {
        // PRUDP type 5 on the NAT echo stream with A5 02 for its payload: what the game sends from
        // UDP 2346 to learn the address it is seen from (grfs/nat.py). Both PRUDP ports answer it,
        // with a bare datagram: A5 02 "udp:/address=..;port=..\0".
        private static readonly byte[] NatEchoQuestion =
            { 0x71, 0x71, 0x05, 0x00, 0, 0, 0, 0, 0, 0, 0xA5, 0x02, 0x8E };

        // The NAT type detector's first question, "what address do you see me from" (grfs/punch.py):
        // 00 0D | 00 01 | seq | 00 | seven bytes of anything. The answer is 19 bytes.
        private static readonly byte[] PunchQuestion =
            { 0x00, 0x0D, 0x00, 0x01, 0x01, 0x00, 0x47, 0x52, 0x46, 0x53, 0x00, 0x00, 0x00 };

        private const int SioUdpConnReset = -1744830452;   // 0x9800000C

        public static bool NatEcho(IPAddress from, IPEndPoint to, out string seenAs, out string error)
        {
            seenAs = null;
            byte[] answer = Exchange(from, to, NatEchoQuestion, out error);
            if (answer == null)
                return false;
            if (answer.Length < 3 || answer[0] != 0xA5 || answer[1] != 0x02)
            {
                error = "пришёл ответ, но не NAT echo";
                return false;
            }
            string url = Encoding.ASCII.GetString(answer, 2, answer.Length - 2).TrimEnd('\0');
            Match m = Regex.Match(url, @"address=([^;]+);port=(\d+)");
            seenAs = m.Success ? m.Groups[1].Value + ":" + m.Groups[2].Value : url;
            return true;
        }

        public static bool Punch(IPEndPoint to, out string seenAs, out string error)
        {
            seenAs = null;
            byte[] a = Exchange(null, to, PunchQuestion, out error);
            if (a == null)
                return false;
            if (a.Length != 19 || a[0] != 0 || a[1] != 0x13 || a[3] != 0x01)
            {
                error = "пришёл ответ, но не от определителя NAT";
                return false;
            }
            seenAs = a[16] + "." + a[15] + "." + a[14] + "." + a[13] + ":" + ((a[17] << 8) | a[18]);
            return true;
        }

        /// <summary>Sends `question` from a socket on `from` (any address when null) until
        /// something comes back from the port it went to; four tries, 0.7 s each.</summary>
        private static byte[] Exchange(IPAddress from, IPEndPoint to, byte[] question, out string error)
        {
            error = null;
            using (Socket s = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
            {
                try
                {
                    // Otherwise an ICMP "port unreachable" fails the next receive.
                    s.IOControl(SioUdpConnReset, new byte[4], null);
                }
                catch (SocketException)
                {
                }
                try
                {
                    s.Bind(new IPEndPoint(from ?? IPAddress.Any, 0));
                }
                catch (SocketException e)
                {
                    error = "сокет на " + from + " не открывается: " + e.Message;
                    return null;
                }
                s.ReceiveTimeout = 700;
                byte[] buffer = new byte[2048];
                for (int attempt = 0; attempt < 4; attempt++)
                {
                    try
                    {
                        s.SendTo(question, to);
                    }
                    catch (SocketException e)
                    {
                        error = e.Message;
                        return null;
                    }
                    DateTime until = DateTime.UtcNow.AddMilliseconds(700);
                    while (DateTime.UtcNow < until)
                    {
                        EndPoint sender = new IPEndPoint(IPAddress.Any, 0);
                        int n;
                        try
                        {
                            n = s.ReceiveFrom(buffer, ref sender);
                        }
                        catch (SocketException e)
                        {
                            if (e.SocketErrorCode == SocketError.TimedOut)
                                break;
                            if (e.SocketErrorCode == SocketError.ConnectionReset)
                                continue;
                            error = e.Message;
                            return null;
                        }
                        // The source address is not compared: a server with several addresses may
                        // answer from another one, and the game does not compare it either.
                        if (((IPEndPoint)sender).Port != to.Port)
                            continue;
                        byte[] answer = new byte[n];
                        Array.Copy(buffer, answer, n);
                        return answer;
                    }
                }
                error = "ответа нет за 3 с";
                return null;
            }
        }
    }

    // ===== this machine ===========================================================================
    internal sealed class LocalView
    {
        public IPAddress Route;       // the address this machine sends to the server from
        public IPAddress First;       // the first one its name resolves to: where the game binds UDP 2346
        public bool NeedsAddress;     // the game has to be started with Route for that

        public static LocalView Look(ServerView server)
        {
            LocalView v = new LocalView();
            Report.Section("Этот компьютер");
            IPEndPoint secure = new IPEndPoint(server.Advertised, server.SecurePort);
            v.Route = Net.LocalAddressFor(secure);
            if (v.Route != null)
            {
                Report.Note("К серверу пакеты уходят с адреса " + v.Route + Named(Net.AdapterName(v.Route)) + ".");
                if (Net.IsPrivate(server.Advertised) && !IPAddress.IsLoopback(v.Route) &&
                    !Net.SameNetwork(v.Route, server.Advertised))
                    Report.Warn("Сервер " + server.Advertised + " не в одной сети с этим адресом: к нему идут через шлюз или VPN.\n" +
                                "Если включён VPN (AmneziaVPN и т. п.), исключи из туннеля " +
                                ServerView.Subnet(server.Advertised) + " (раздельное туннелирование).");
            }
            string name;
            v.First = Net.FirstHostAddress(out name);
            if (v.First == null)
            {
                Report.Warn("Не удалось узнать, на какой адрес игра откроет UDP 2346: имя «" + name + "» не находится.");
                return v;
            }
            string adapter = Named(Net.AdapterName(v.First));
            if (IPAddress.IsLoopback(secure.Address))
            {
                // Any address of this machine reaches a server on it; what matters about this one
                // is that the other players can reach it.
                Report.Note("Сервер на этом же компьютере. Игра откроет UDP 2346 на " + v.First + adapter +
                            ":\nпо этому адресу к ней будут подключаться другие игроки.");
                return v;
            }
            if (!server.SecureOk)
            {
                Report.Note("Игра откроет UDP 2346 на " + v.First + adapter + ".");
                return v;
            }
            string seen, why, whyRoute;
            if (Probe.NatEcho(v.First, secure, out seen, out why))
            {
                Report.Ok("Игра откроет UDP 2346 на " + v.First + adapter + " - с него сервер виден.");
                return v;
            }
            if (v.Route != null && !v.Route.Equals(v.First) && Probe.NatEcho(v.Route, secure, out seen, out whyRoute))
            {
                v.NeedsAddress = true;
                Report.Warn("Игра откроет UDP 2346 на " + v.First + adapter + ", а с этого адреса сервер не виден (" + why + ").\n" +
                            "С таким адресом вход в чужую сессию виснет на «Joining Session». Поэтому игру надо\n" +
                            "запускать отсюда: она получит адрес " + v.Route + " (переменная окружения " + Game.AddressVariable + ";\n" +
                            "в самой игре ничего не меняется).");
            }
            else
                Report.Bad("С адреса " + v.First + adapter + ", на котором игра откроет UDP 2346, сервер не виден: " + why + ".");
            return v;
        }

        public static string Named(string adapter)
        {
            return adapter == null ? "" : " («" + adapter + "»)";
        }
    }

    /// <summary>What has to change on this machine, and the rest of what the game needs here.</summary>
    internal sealed class Needs
    {
        public bool HostsChange, RuleMissing;
        public List<string> Blocks = new List<string>();

        public static Needs Look(IPAddress address, string current)
        {
            Needs n = new Needs();
            Report.Section("Что нужно игре на этом компьютере");
            n.HostsChange = Hosts.Changes(Hosts.SystemPath, address.ToString());
            if (!n.HostsChange)
                Report.Ok("hosts уже ведёт " + Hosts.Name + " на " + address + ".");
            else
                Report.Note("hosts: нужна строка «" + address + " " + Hosts.Name + "»" +
                            (current != null && current != address.ToString() ? " (сейчас там " + current + ")" : "") + ".");
            try
            {
                n.RuleMissing = !Firewall.HasRule();
                n.Blocks = Firewall.GameBlocks();
                if (n.RuleMissing)
                    Report.Note("Брандмауэр: правила для входящих UDP 2346-2347 нет (по ним игры соединяются друг с другом)\n" +
                                "- его добавит подключение.");
                else
                    Report.Ok("Брандмауэр пускает входящие UDP 2346-2347 - по ним игры соединяются друг с другом.");
                if (n.Blocks.Count > 0)
                    Report.Warn("Брандмауэр запрещает игре входящие соединения: " + string.Join(", ", n.Blocks) + ".\n" +
                                "Такие правила Windows добавляет, если в её окне про доступ к сети нажали «Отмена»,\n" +
                                "а запрет сильнее любого разрешения.");
            }
            catch (Exception e)
            {
                n.RuleMissing = true;
                Report.Warn("Правила брандмауэра не прочитать: " + e.Message);
            }
            if (Game.UbisoftConnect() == null)
                Report.Warn("Ubisoft Connect не установлен, а без него игра не запустится. Установщик лежит в папке\n" +
                            "игры: UbisoftConnectInstaller.exe.");
            else
                Report.Ok("Ubisoft Connect установлен" + (Game.UbisoftConnectRunning()
                    ? " и запущен."
                    : "; перед игрой он должен быть запущен, с входом в аккаунт."));
            string folder = Game.Find();
            if (folder != null)
                Report.Ok("Игра: " + folder + ".");
            else
                Report.Note("Игры нет в библиотеках Steam - папку можно будет указать при запуске.");
            return n;
        }
    }

    // ===== the changes ==========================================================================
    internal static class Elevation
    {
        public static bool IsAdmin()
        {
            using (WindowsIdentity me = WindowsIdentity.GetCurrent())
                return new WindowsPrincipal(me).IsInRole(WindowsBuiltInRole.Administrator);
        }

        /// <summary>Does `action` - "connect ADDRESS [--unblock]" or "disconnect" - with
        /// administrator rights and shows what was done. Already elevated, it happens here;
        /// otherwise this program starts again with --elevated, which is when Windows asks.</summary>
        public static bool Apply(string action)
        {
            string[] words = action.Split(' ');
            if (IsAdmin())
            {
                List<string> said = new List<string>();
                bool ok = Steps.Run(words[0], words.Length > 1 ? words[1] : null, words.Contains("--unblock"),
                                    Hosts.SystemPath, said);
                foreach (string line in said)
                    Report.Relay(line);
                return ok;
            }
            string result = Path.Combine(Path.GetTempPath(), "grfs-connect-" + Guid.NewGuid().ToString("N") + ".txt");
            ProcessStartInfo start = new ProcessStartInfo(Assembly.GetExecutingAssembly().Location,
                                                          "--elevated \"" + result + "\" " + action);
            start.UseShellExecute = true;
            start.Verb = "runas";
            start.WindowStyle = ProcessWindowStyle.Hidden;
            Report.Note("Для hosts и брандмауэра нужны права администратора - Windows сейчас спросит.");
            int code;
            try
            {
                using (Process p = Process.Start(start))
                {
                    p.WaitForExit();
                    code = p.ExitCode;
                }
            }
            catch (Win32Exception e)
            {
                Report.Bad(e.NativeErrorCode == 1223
                    ? "Права администратора не даны - ничего не изменено."
                    : "С правами администратора не запуститься: " + e.Message);
                return false;
            }
            if (!File.Exists(result))
            {
                Report.Bad("Часть с правами администратора ничего не сообщила (код " + code + ").");
                return false;
            }
            foreach (string line in File.ReadAllLines(result, Encoding.UTF8))
                Report.Relay(line);
            File.Delete(result);
            return code == 0;
        }

        /// <summary>The elevated half: does the action and writes what happened to the file named
        /// after --elevated, for the half that asked.</summary>
        public static int Serve(Options o)
        {
            List<string> said = new List<string>();
            bool ok;
            try
            {
                ok = Steps.Run(o.Action, o.Address, o.Unblock, Hosts.SystemPath, said);
            }
            catch (Exception e)
            {
                said.Add("!" + e.Message);
                ok = false;
            }
            try
            {
                // A line each: a system message with a line break in it would come back as two.
                string[] lines = said.Select(l => Regex.Replace(l, @"\s*[\r\n]+\s*", " ").TrimEnd()).ToArray();
                File.WriteAllLines(o.ResultFile, lines, new UTF8Encoding(false));
            }
            catch (Exception)
            {
                return 3;
            }
            return ok ? 0 : 1;
        }
    }

    /// <summary>The steps that need administrator rights. Each says what it did in `said`, a line
    /// each, the first character being the mark the report shows.</summary>
    internal static class Steps
    {
        public static bool Run(string action, string address, bool unblock, string hostsPath, List<string> said)
        {
            bool ok;
            if (action == "connect")
            {
                ok = PointHosts(hostsPath, address, said);
                FlushDns(said);
                ok &= AddRule(said);
                if (unblock)
                    ok &= Unblock(said);
            }
            else
            {
                ok = UnpointHosts(hostsPath, said);
                FlushDns(said);
                ok &= RemoveRule(said);
            }
            return ok;
        }

        public static bool PointHosts(string path, string address, List<string> said)
        {
            try
            {
                Encoding encoding;
                List<string> lines = Hosts.Read(path, out encoding);
                List<string> wanted = Hosts.Connect(lines, address);
                if (wanted.SequenceEqual(lines))
                {
                    said.Add("+hosts уже ведёт " + Hosts.Name + " на " + address + ".");
                    return true;
                }
                Hosts.Write(path, wanted, encoding);
                bool disabled = wanted.Count(l => l.StartsWith(Hosts.Disabled)) > lines.Count(l => l.StartsWith(Hosts.Disabled));
                said.Add("+hosts: добавлена строка «" + address + " " + Hosts.Name + "»." +
                         (disabled ? " Прежняя строка для этого имени закомментирована (" + Hosts.Disabled.Trim() + ")." : ""));
                return true;
            }
            catch (Exception e)
            {
                said.Add("!hosts не записан: " + e.Message);
                return false;
            }
        }

        public static bool UnpointHosts(string path, List<string> said)
        {
            try
            {
                Encoding encoding;
                List<string> lines = Hosts.Read(path, out encoding);
                List<string> wanted = Hosts.Disconnect(lines);
                if (wanted.SequenceEqual(lines))
                {
                    said.Add(" hosts: строки GRFS Connect там нет.");
                    return true;
                }
                Hosts.Write(path, wanted, encoding);
                said.Add("+hosts: строка GRFS Connect убрана.");
                return true;
            }
            catch (Exception e)
            {
                said.Add("!hosts не записан: " + e.Message);
                return false;
            }
        }

        private static void FlushDns(List<string> said)
        {
            bool flushed;
            try
            {
                flushed = Native.DnsFlushResolverCache() != 0;
            }
            catch (Exception)
            {
                flushed = false;
            }
            said.Add(flushed ? " Кэш DNS сброшен."
                             : "?Кэш DNS не сбросился: если игра не найдёт сервер, перезагрузи компьютер.");
        }

        private static bool AddRule(List<string> said)
        {
            try
            {
                if (Firewall.HasRule())
                {
                    said.Add("+Брандмауэр: правило «" + Firewall.RuleName + "» уже есть.");
                    return true;
                }
                Firewall.AddRule();
                said.Add("+Брандмауэр: правило «" + Firewall.RuleName + "» пускает входящие UDP 2346-2347.");
                return true;
            }
            catch (Exception e)
            {
                said.Add("!Брандмауэр: правило не добавлено: " + e.Message);
                return false;
            }
        }

        private static bool Unblock(List<string> said)
        {
            try
            {
                said.Add("+Брандмауэр: отключено запрещающих правил для игры - " + Firewall.Unblock() + ".");
                return true;
            }
            catch (Exception e)
            {
                said.Add("!Брандмауэр: запрещающие правила не отключены: " + e.Message);
                return false;
            }
        }

        private static bool RemoveRule(List<string> said)
        {
            try
            {
                if (!Firewall.HasRule())
                {
                    said.Add(" Брандмауэр: правила GRFS Connect там нет.");
                    return true;
                }
                Firewall.RemoveRule();
                said.Add("+Брандмауэр: правило «" + Firewall.RuleName + "» убрано.");
                return true;
            }
            catch (Exception e)
            {
                said.Add("!Брандмауэр: правило не убрано: " + e.Message);
                return false;
            }
        }
    }

    /// <summary>The hosts file. Only lines that map onlineconfigservice.ubi.com are touched.</summary>
    internal static class Hosts
    {
        public const string Name = "onlineconfigservice.ubi.com";
        // The first line of the block. The GRFSConnect of the old C# server wrote the same one, so
        // each program replaces the other's block instead of adding a second.
        public const string Marker = "# GRFSConnect - remove this block to go back to the real Ubisoft servers";
        // In front of a line for another address, the way tools/setup_once.ps1 switches one off:
        // kept, so that it can be put back by hand.
        public const string Disabled = "# (grfs) ";

        public static string SystemPath
        {
            get
            {
                return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System),
                                    "drivers", "etc", "hosts");
            }
        }

        /// <summary>The address the first active line for the name gives, or null.</summary>
        public static string CurrentAddress(string path)
        {
            try
            {
                Encoding encoding;
                foreach (string line in Read(path, out encoding))
                {
                    string[] fields = Fields(line);
                    if (fields != null && HasName(fields))
                        return fields[0];
                }
            }
            catch (IOException)
            {
            }
            catch (UnauthorizedAccessException)
            {
            }
            return null;
        }

        /// <summary>Whether Connect (with an address) or Disconnect (with null) would change it.</summary>
        public static bool Changes(string path, string address)
        {
            try
            {
                Encoding encoding;
                List<string> lines = Read(path, out encoding);
                return !(address == null ? Disconnect(lines) : Connect(lines, address)).SequenceEqual(lines);
            }
            catch (IOException)
            {
                return true;
            }
            catch (UnauthorizedAccessException)
            {
                return true;
            }
        }

        /// <summary>Our block at the end, with the address; the block from before goes, and so does
        /// any other line for the name - commented out, or, when it maps other names too, without
        /// this one. Windows takes the first match, so a stale line above ours would win. A file
        /// whose one line for the name already has the address stays as it is, whoever wrote it.</summary>
        public static List<string> Connect(IList<string> lines, string address)
        {
            List<string[]> mapped = lines.Select(Fields).Where(f => f != null && HasName(f)).ToList();
            if (mapped.Count == 1 && mapped[0][0] == address)
                return new List<string>(lines);
            List<string> result = new List<string>();
            bool ours = false;
            foreach (string line in lines)
            {
                if (line.Trim() == Marker)
                {
                    ours = true;
                    continue;
                }
                string[] fields = Fields(line);
                if (fields != null && HasName(fields))
                {
                    if (!ours)
                        result.Add(fields.Length > 2 ? WithoutName(line, fields) : Disabled + line);
                    ours = false;
                    continue;
                }
                ours = false;
                result.Add(line);
            }
            TrimEnd(result);
            result.Add("");
            result.Add(Marker);
            result.Add(address + " " + Name);
            return result;
        }

        /// <summary>Without our block. Lines switched off on the way in stay off: what they pointed
        /// at was not wanted then either.</summary>
        public static List<string> Disconnect(IList<string> lines)
        {
            List<string> result = new List<string>();
            bool ours = false;
            foreach (string line in lines)
            {
                if (line.Trim() == Marker)
                {
                    ours = true;
                    continue;
                }
                string[] fields = Fields(line);
                if (ours && fields != null && HasName(fields))
                {
                    ours = false;
                    continue;
                }
                ours = false;
                result.Add(line);
            }
            TrimEnd(result);
            return result;
        }

        /// <summary>The fields before any comment, or null when there is no mapping on the line.</summary>
        private static string[] Fields(string line)
        {
            int hash = line.IndexOf('#');
            string active = hash >= 0 ? line.Substring(0, hash) : line;
            string[] fields = active.Split(new[] { ' ', '\t' }, StringSplitOptions.RemoveEmptyEntries);
            return fields.Length >= 2 ? fields : null;
        }

        private static bool HasName(string[] fields)
        {
            for (int i = 1; i < fields.Length; i++)
                if (string.Equals(fields[i], Name, StringComparison.OrdinalIgnoreCase))
                    return true;
            return false;
        }

        private static string WithoutName(string line, string[] fields)
        {
            int hash = line.IndexOf('#');
            string comment = hash >= 0 ? " " + line.Substring(hash) : "";
            return string.Join(" ", fields.Where(f => !string.Equals(f, Name, StringComparison.OrdinalIgnoreCase))) +
                   comment;
        }

        private static void TrimEnd(List<string> lines)
        {
            while (lines.Count > 0 && lines[lines.Count - 1].Trim().Length == 0)
                lines.RemoveAt(lines.Count - 1);
        }

        /// <summary>The lines, and the encoding to write them back in: UTF-8 (with its BOM if it
        /// had one) when the bytes are UTF-8, the ANSI code page otherwise - which is what Notepad
        /// saved an older file in.</summary>
        public static List<string> Read(string path, out Encoding encoding)
        {
            byte[] raw = File.ReadAllBytes(path);
            bool bom = raw.Length >= 3 && raw[0] == 0xEF && raw[1] == 0xBB && raw[2] == 0xBF;
            string text;
            try
            {
                text = new UTF8Encoding(false, true).GetString(raw, bom ? 3 : 0, raw.Length - (bom ? 3 : 0));
                encoding = new UTF8Encoding(bom);
            }
            catch (DecoderFallbackException)
            {
                encoding = Encoding.Default;
                text = encoding.GetString(raw);
            }
            List<string> lines = new List<string>(text.Split(new[] { "\r\n", "\n" }, StringSplitOptions.None));
            if (lines.Count > 0 && lines[lines.Count - 1].Length == 0)
                lines.RemoveAt(lines.Count - 1);
            return lines;
        }

        public static void Write(string path, IList<string> lines, Encoding encoding)
        {
            FileAttributes attributes = File.GetAttributes(path);
            bool readOnly = (attributes & FileAttributes.ReadOnly) != 0;
            if (readOnly)
                File.SetAttributes(path, attributes & ~FileAttributes.ReadOnly);
            try
            {
                File.WriteAllText(path, string.Join("\r\n", lines) + "\r\n", encoding);
            }
            finally
            {
                if (readOnly)
                    File.SetAttributes(path, attributes);
            }
        }
    }

    /// <summary>Windows Firewall, through its own COM objects: reading the rules needs no rights,
    /// and nothing here depends on the language netsh would answer in.</summary>
    internal static class Firewall
    {
        public const string RuleName = "GRFS co-op (UDP 2346-2347)";
        private const int Inbound = 1, Block = 0, Allow = 1, Udp = 17, AllProfiles = 0x7FFFFFFF;
        private static readonly string[] GameFiles =
            { "Future Soldier DX11.exe", "Future Soldier DX9.exe", "Future Soldier.exe" };

        private static dynamic Rules()
        {
            dynamic policy = Activator.CreateInstance(Type.GetTypeFromProgID("HNetCfg.FwPolicy2"));
            return policy.Rules;
        }

        public static bool HasRule()
        {
            foreach (dynamic rule in Rules())
                if ((string)rule.Name == RuleName)
                    return true;
            return false;
        }

        /// <summary>Enabled inbound block rules for the game: what Windows adds when its prompt to
        /// let the game on the network is cancelled. A block rule wins over any allow rule.</summary>
        public static List<string> GameBlocks()
        {
            List<string> found = new List<string>();
            foreach (dynamic rule in Rules())
                if (IsGameBlock(rule))
                    found.Add("«" + (string)rule.Name + "»");
            return found;
        }

        public static int Unblock()
        {
            int count = 0;
            foreach (dynamic rule in Rules())
            {
                if (!IsGameBlock(rule))
                    continue;
                rule.Enabled = false;
                count++;
            }
            return count;
        }

        public static void AddRule()
        {
            dynamic rule = Activator.CreateInstance(Type.GetTypeFromProgID("HNetCfg.FWRule"));
            rule.Name = RuleName;
            rule.Description = "Ghost Recon Future Soldier: games reach each other directly on UDP 2346 and 2347. Added by GRFS Connect.";
            rule.Grouping = "GRFS";
            rule.Protocol = Udp;            // first: ports cannot be set on "any protocol"
            rule.LocalPorts = "2346-2347";
            rule.Direction = Inbound;
            rule.Action = Allow;
            rule.Profiles = AllProfiles;
            rule.Enabled = true;
            Rules().Add(rule);
        }

        public static void RemoveRule()
        {
            for (int i = 0; i < 16 && HasRule(); i++)
                Rules().Remove(RuleName);
            if (HasRule())
                throw new InvalidOperationException("оно на месте и после удаления");
        }

        private static bool IsGameBlock(object item)
        {
            dynamic rule = item;
            if ((int)rule.Direction != Inbound || (int)rule.Action != Block || !(bool)rule.Enabled)
                return false;
            string application = (string)rule.ApplicationName;
            if (string.IsNullOrEmpty(application))
                return false;
            // Not Path.GetFileName: it throws on characters a rule is free to have.
            string file = application.Substring(application.LastIndexOfAny(new[] { '\\', '/' }) + 1);
            return GameFiles.Any(g => string.Equals(g, file, StringComparison.OrdinalIgnoreCase));
        }
    }

    // ===== the game =============================================================================
    internal static class Game
    {
        public const string FolderName = "Tom Clancy's Ghost Recon Future Soldier";
        // The small launcher: it starts the DX11 or the DX9 build, and what it starts inherits the
        // environment.
        public const string Launcher = "Future Soldier.exe";
        public const string AddressVariable = "_CLUSTER_NETWORK_NAME_";
        private const int P2pPort = 2346;
        private static readonly string[] Processes = { "Future Soldier DX11", "Future Soldier DX9" };

        /// <summary>Beside this program or a few folders above it (the game folder itself, or
        /// GRFS-Emulator\dist in it), then every Steam library.</summary>
        public static string Find()
        {
            string here = AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\');
            for (int i = 0; i < 3 && !string.IsNullOrEmpty(here); i++)
            {
                if (File.Exists(Path.Combine(here, Launcher)))
                    return here;
                here = Path.GetDirectoryName(here);
            }
            foreach (string library in SteamLibraries())
            {
                string folder = Path.Combine(library, "steamapps", "common", FolderName);
                if (File.Exists(Path.Combine(folder, Launcher)))
                    return folder;
            }
            return null;
        }

        private static List<string> SteamLibraries()
        {
            List<string> roots = new List<string>();
            AddFolder(roots, RegistryText(@"HKEY_CURRENT_USER\Software\Valve\Steam", "SteamPath"));
            AddFolder(roots, RegistryText(@"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"));
            AddFolder(roots, RegistryText(@"HKEY_LOCAL_MACHINE\SOFTWARE\Valve\Steam", "InstallPath"));
            List<string> libraries = new List<string>(roots);
            foreach (string root in roots)
            {
                try
                {
                    string vdf = File.ReadAllText(Path.Combine(root, "steamapps", "libraryfolders.vdf"));
                    foreach (Match m in Regex.Matches(vdf, "\"path\"\\s+\"([^\"]+)\""))
                        AddFolder(libraries, m.Groups[1].Value.Replace("\\\\", "\\"));
                }
                catch (IOException)
                {
                }
                catch (UnauthorizedAccessException)
                {
                }
            }
            return libraries;
        }

        private static void AddFolder(List<string> list, string path)
        {
            if (string.IsNullOrEmpty(path))
                return;
            try
            {
                path = Path.GetFullPath(path);
            }
            catch (Exception)
            {
                return;
            }
            if (!list.Contains(path, StringComparer.OrdinalIgnoreCase))
                list.Add(path);
        }

        private static string RegistryText(string key, string value)
        {
            try
            {
                return Registry.GetValue(key, value, null) as string;
            }
            catch (Exception)
            {
                return null;
            }
        }

        public static string UbisoftConnect()
        {
            List<string> candidates = new List<string>();
            string installed = RegistryText(@"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Ubisoft\Launcher", "InstallDir");
            if (!string.IsNullOrEmpty(installed))
                candidates.Add(installed);
            foreach (string programs in new[] { Environment.GetEnvironmentVariable("ProgramFiles(x86)"),
                                                Environment.GetEnvironmentVariable("ProgramFiles") })
                if (!string.IsNullOrEmpty(programs))
                    candidates.Add(Path.Combine(programs, "Ubisoft", "Ubisoft Game Launcher"));
            return candidates.FirstOrDefault(Directory.Exists);
        }

        public static bool UbisoftConnectRunning()
        {
            Process[] found = Process.GetProcessesByName("upc");
            foreach (Process p in found)
                p.Dispose();
            return found.Length > 0;
        }

        public static bool Running()
        {
            return GamePids().Count > 0;
        }

        /// <summary>Starts the launcher, with the address for UDP 2346 when one is given. Without
        /// one the variable is removed rather than inherited from wherever this program got it.</summary>
        public static void Start(string folder, IPAddress address)
        {
            ProcessStartInfo start = new ProcessStartInfo(Path.Combine(folder, Launcher));
            start.WorkingDirectory = folder;
            start.UseShellExecute = false;    // the only way to give it an environment of its own
            if (address != null)
                start.EnvironmentVariables[AddressVariable] = address.ToString();
            else
                start.EnvironmentVariables.Remove(AddressVariable);
            Process.Start(start).Dispose();
        }

        /// <summary>Says where the game binds UDP 2346: the one thing that shows whether the
        /// address got through. It binds it some time after it starts (in the menus it has only
        /// 2347), so for a game started here this waits for as long as the game runs; for one
        /// already running (`startedHere` false) it looks once.</summary>
        public static void Watch(IPAddress expected, bool pinned, bool startedHere)
        {
            if (startedHere)
                Report.Note("UDP 2346 игра открывает не сразу после запуска. Это окно дождётся и скажет, на каком\n" +
                            "адресе; закрыть его можно когда угодно - на игру это не влияет.");
            DateTime started = DateTime.UtcNow;
            DateTime? gone = null;
            bool seen = false;
            while (true)
            {
                HashSet<int> pids = GamePids();
                if (pids.Count > 0)
                {
                    seen = true;
                    gone = null;
                }
                else if (!seen && startedHere && DateTime.UtcNow - started > TimeSpan.FromMinutes(3))
                {
                    Report.Warn("Игра не появилась за 3 минуты.");
                    return;
                }
                else if (seen)
                {
                    // Closed - or being started again, which Ubisoft Connect does to a game now and then.
                    if (gone == null)
                        gone = DateTime.UtcNow;
                    else if (DateTime.UtcNow - gone.Value > TimeSpan.FromSeconds(20))
                    {
                        Report.Note("Игра закрыта, UDP 2346 она так и не открыла.");
                        return;
                    }
                }
                List<Net.UdpRow> sockets = pids.Count > 0 ? Net.UdpSockets() : new List<Net.UdpRow>();
                foreach (Net.UdpRow row in sockets)
                {
                    if (row.Port != P2pPort || !pids.Contains(row.Pid))
                        continue;
                    string adapter = LocalView.Named(Net.AdapterName(row.Address));
                    string at = "Игра открыла UDP 2346 на " + row.Address + adapter;
                    if (expected == null || row.Address.Equals(expected))
                        Report.Ok(at + (expected != null ? " - как надо." : "."));
                    else if (pinned && !startedHere)
                        Report.Warn(at + ", а нужен " + expected + ": закрой её и запусти отсюда.");
                    else if (pinned)
                        Report.Bad(at + ", а не на " + expected + ": адрес до неё не дошёл.\n" +
                                   "Так бывает, если игру перезапустил Ubisoft Connect. Закрой игру и Ubisoft Connect и\n" +
                                   "запусти этот exe ещё раз; если не поможет - отключи на время игры адаптер" + adapter + ".");
                    else
                        Report.Warn(at + ", а ожидался " + expected + ".");
                    foreach (Net.UdpRow other in sockets)
                        if (other.Port == P2pPort + 1 && pids.Contains(other.Pid))
                            Report.Note("UDP 2347 у игры - на " + other.Address + ".");
                    return;
                }
                if (!startedHere)
                {
                    Report.Note("UDP 2346 у игры пока нет: она открывает его не сразу после запуска.");
                    return;
                }
                Thread.Sleep(1000);
            }
        }

        private static HashSet<int> GamePids()
        {
            HashSet<int> pids = new HashSet<int>();
            foreach (string name in Processes)
                foreach (Process p in Process.GetProcessesByName(name))
                {
                    pids.Add(p.Id);
                    p.Dispose();
                }
            return pids;
        }
    }

    // ===== winsock and the rest ===================================================================
    internal static class Net
    {
        public struct UdpRow
        {
            public IPAddress Address;
            public int Port;
            public int Pid;
        }

        /// <summary>Which of this machine's addresses the system sends from to reach `target`.
        /// Connecting a UDP socket sends nothing; it only picks the route.</summary>
        public static IPAddress LocalAddressFor(IPEndPoint target)
        {
            using (Socket s = new Socket(AddressFamily.InterNetwork, SocketType.Dgram, ProtocolType.Udp))
            {
                try
                {
                    s.Connect(target);
                    return ((IPEndPoint)s.LocalEndPoint).Address;
                }
                catch (SocketException)
                {
                    return null;
                }
            }
        }

        public static string AdapterName(IPAddress address)
        {
            string name;
            Find(address, out name);
            return name;
        }

        /// <summary>Whether `other` is on the network of our address `local`: reached directly,
        /// not through a gateway.</summary>
        public static bool SameNetwork(IPAddress local, IPAddress other)
        {
            string name;
            UnicastIPAddressInformation info = Find(local, out name);
            if (info == null || info.IPv4Mask == null)
                return false;
            byte[] a = local.GetAddressBytes(), b = other.GetAddressBytes(), mask = info.IPv4Mask.GetAddressBytes();
            for (int i = 0; i < 4; i++)
                if ((a[i] & mask[i]) != (b[i] & mask[i]))
                    return false;
            return true;
        }

        public static bool IsPrivate(IPAddress address)
        {
            byte[] b = address.GetAddressBytes();
            return b[0] == 10 || (b[0] == 172 && b[1] >= 16 && b[1] <= 31) || (b[0] == 192 && b[1] == 168);
        }

        private static UnicastIPAddressInformation Find(IPAddress address, out string adapter)
        {
            adapter = null;
            if (address == null)
                return null;
            foreach (NetworkInterface ni in NetworkInterface.GetAllNetworkInterfaces())
                foreach (UnicastIPAddressInformation info in ni.GetIPProperties().UnicastAddresses)
                    if (info.Address.Equals(address))
                    {
                        adapter = ni.Name;
                        return info;
                    }
            return null;
        }

        /// <summary>The first address this machine's name resolves to, asked of winsock the way
        /// the game asks it - gethostname, gethostbyname, the first entry - which is what it binds
        /// UDP 2346 to. .NET's own resolver lists the addresses in another order and cannot stand
        /// in for it.</summary>
        public static IPAddress FirstHostAddress(out string name)
        {
            Native.WSAStartup(0x0202, new byte[512]);
            StringBuilder buffer = new StringBuilder(256);
            name = Native.gethostname(buffer, buffer.Capacity) == 0 ? buffer.ToString() : Dns.GetHostName();
            IntPtr found = Native.gethostbyname(name);
            if (found == IntPtr.Zero)
                return null;
            Native.HostEnt entry = (Native.HostEnt)Marshal.PtrToStructure(found, typeof(Native.HostEnt));
            if (entry.AddressType != 2 || entry.AddressList == IntPtr.Zero)
                return null;
            IntPtr first = Marshal.ReadIntPtr(entry.AddressList);
            if (first == IntPtr.Zero)
                return null;
            byte[] bytes = new byte[4];
            Marshal.Copy(first, bytes, 0, 4);
            return new IPAddress(bytes);
        }

        /// <summary>Every IPv4 UDP socket on this machine, with the process that holds it.</summary>
        public static List<UdpRow> UdpSockets()
        {
            List<UdpRow> rows = new List<UdpRow>();
            int size = 0;
            Native.GetExtendedUdpTable(IntPtr.Zero, ref size, false, 2, 1, 0);
            size += 4096;   // room for sockets opened between the two calls
            IntPtr table = Marshal.AllocHGlobal(size);
            try
            {
                if (Native.GetExtendedUdpTable(table, ref size, false, 2, 1, 0) != 0)
                    return rows;
                int count = Marshal.ReadInt32(table);
                for (int i = 0; i < count; i++)
                {
                    IntPtr row = IntPtr.Add(table, 4 + i * 12);
                    int port = Marshal.ReadInt32(row, 4);
                    UdpRow r = new UdpRow();
                    r.Address = new IPAddress((long)(uint)Marshal.ReadInt32(row));
                    r.Port = ((port & 0xFF) << 8) | ((port >> 8) & 0xFF);
                    r.Pid = Marshal.ReadInt32(row, 8);
                    rows.Add(r);
                }
            }
            finally
            {
                Marshal.FreeHGlobal(table);
            }
            return rows;
        }
    }

    internal static class Native
    {
        [StructLayout(LayoutKind.Sequential)]
        public struct HostEnt
        {
            public IntPtr Name;
            public IntPtr Aliases;
            public short AddressType;
            public short Length;
            public IntPtr AddressList;
        }

        [DllImport("ws2_32.dll")]
        public static extern int WSAStartup(ushort version, byte[] data);

        [DllImport("ws2_32.dll", CharSet = CharSet.Ansi, BestFitMapping = false)]
        public static extern int gethostname(StringBuilder name, int length);

        [DllImport("ws2_32.dll", CharSet = CharSet.Ansi, BestFitMapping = false)]
        public static extern IntPtr gethostbyname(string name);

        [DllImport("iphlpapi.dll")]
        public static extern uint GetExtendedUdpTable(IntPtr table, ref int size, bool order, int family,
                                                      int tableClass, int reserved);

        [DllImport("dnsapi.dll")]
        public static extern int DnsFlushResolverCache();
    }
}

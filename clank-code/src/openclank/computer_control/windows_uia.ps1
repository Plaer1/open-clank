# Open Clank Windows (x64) control helper: UI Automation for reading and acting on elements, SendInput for pointer and keys.
# Speaks the SAME JSON Lines protocol as the Linux helper (`--a11y-v1` / `--control-v1`): one request per line on stdin, one reply per line on stdout.
# Written for this project from the behaviours described in our study notes; it shares no text with any other implementation.
# NOT RUN on a real Windows desktop (the author's machine is Linux). Operations outside the minimum answer `unsupported_action`, never a silent no-op.
param([ValidateSet('a11y', 'control')][string]$Mode = 'a11y')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes, System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class OcInput {
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public IntPtr dwExtraInfo; }
  [StructLayout(LayoutKind.Explicit)] public struct U { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public U u; }
  [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int size);
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
  public static bool Key(ushort vk, bool up) {
    INPUT[] a = new INPUT[1]; a[0].type = 1; a[0].u.ki.wVk = vk; a[0].u.ki.dwFlags = up ? 2u : 0u;
    return SendInput(1, a, Marshal.SizeOf(typeof(INPUT))) == 1; }
  public static bool Char(char c, bool up) {
    INPUT[] a = new INPUT[1]; a[0].type = 1; a[0].u.ki.wScan = c; a[0].u.ki.dwFlags = 4u | (up ? 2u : 0u);
    return SendInput(1, a, Marshal.SizeOf(typeof(INPUT))) == 1; }
  public static bool Button(uint flags) {
    INPUT[] a = new INPUT[1]; a[0].type = 0; a[0].u.mi.dwFlags = flags;
    return SendInput(1, a, Marshal.SizeOf(typeof(INPUT))) == 1; }
}
"@
Add-Type -AssemblyName System.Windows.Forms
$UIA = [System.Windows.Automation.AutomationElement]
$Walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker
$MAX_NODES = 1200; $MAX_DEPTH = 64; $MAX_VALUE = 500; $TTL = 120
$Excluded = @('keepassxc', 'keepass', 'bitwarden', '1password', 'lastpass', 'dashlane', 'enpass', 'proton pass', 'protonpass', 'nordpass', 'roboform', 'psono', 'credential manager')
$SecretWords = @('password', 'passwd', 'passphrase', 'secret', 'token', 'api key', 'apikey', 'api_key', 'private key', 'credential')
$Snaps = @{}
$Held = New-Object System.Collections.ArrayList

function Out-Line($obj) { [Console]::Out.WriteLine(($obj | ConvertTo-Json -Compress -Depth 30)); [Console]::Out.Flush() }
function Bad($code) { Out-Line @{ ok = $false; code = $code } }
function Is-Excluded($name, $title) {
  $t = (("$name " + "$title").ToLower())
  foreach ($f in $Excluded) { if ($t.Contains($f)) { return $true } }
  return $false
}
function Role-Name($e) { ($e.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '').ToLower() }
function Pattern-Names($e) {
  $n = New-Object System.Collections.ArrayList
  foreach ($p in $e.GetSupportedPatterns()) {
    $pn = $p.ProgrammaticName -replace 'Identifiers\.Pattern$', ''
    [void]$n.Add($pn)
  }
  return , @($n)
}
function Action-Names($e) {
  $a = @()
  $names = Pattern-Names $e
  if ($names -contains 'Invoke') { $a += 'Invoke' }
  if ($names -contains 'Toggle') { $a += 'Toggle' }
  if ($names -contains 'SelectionItem') { $a += 'Select' }
  if ($names -contains 'ExpandCollapse') { $a += 'ExpandCollapse' }
  return , $a
}
function Get-Value($e) {
  $p = $null
  if ($e.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p)) { return @{ text = $p.Current.Value; readonly = $p.Current.IsReadOnly } }
  return $null
}
function Looks-Secret($v) {
  $t = "$v".Trim()
  if ($t.StartsWith('-----BEGIN') -or $t.StartsWith('sk-') -or $t.StartsWith('ghp_') -or $t.ToLower().StartsWith('bearer ')) { return $true }
  return ($t.Length -ge 32 -and $t -notmatch '\s' -and $t -match '^[A-Za-z0-9+/=_-]+$')
}
function Node-Info($e) {
  $cur = $e.Current
  $name = "$($cur.Name)"; $role = Role-Name $e
  $info = @{ name = $name; role = $role; id = "$($cur.AutomationId)"; actions = (Action-Names $e); value = $null; protected = [bool]$cur.IsPassword; editable = $false; showing = (-not $cur.IsOffscreen) }
  $v = Get-Value $e
  if ($v -ne $null) { $info.value = $v.text; $info.editable = (-not $v.readonly) }
  return $info
}
function Hide-Value($i) {
  if ($i.protected) { return $true }
  $l = $i.name.ToLower() + ' ' + $i.id.ToLower()
  foreach ($w in $SecretWords) { if ($l.Contains($w)) { return $true } }
  return ($i.value -ne $null -and (Looks-Secret $i.value))
}
function Sig($i) { "$($i.role)|$($i.name)|$($i.id)|" + ($i.actions -join ',') }
function Proc-Start($pid_) { try { return "$((Get-Process -Id $pid_).StartTime.ToFileTimeUtc())" } catch { return '0' } }
function Top-Windows() {
  $kids = $UIA::RootElement.FindAll([System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
  $list = @()
  foreach ($k in $kids) { $list += , $k }
  return , $list
}
function Apps() {
  $by = @{}
  foreach ($w in (Top-Windows)) {
    $pid_ = $w.Current.ProcessId
    if (-not $by.ContainsKey($pid_)) {
      $pn = ''; try { $pn = (Get-Process -Id $pid_).ProcessName } catch { }
      $by[$pid_] = @{ pid = $pid_; name = $pn; windows = New-Object System.Collections.ArrayList }
    }
    [void]$by[$pid_].windows.Add($w)
  }
  return $by
}
function Find-App($sel) {
  $apps = Apps
  foreach ($a in $apps.Values) {
    $hit = $false
    if ($sel.pid -ne $null -and [int]$sel.pid -eq $a.pid) { $hit = $true }
    if ($sel.name -ne $null -and $a.name.ToLower().Contains("$($sel.name)".ToLower())) { $hit = $true }
    if ($hit) {
      $titles = ($a.windows | ForEach-Object { $_.Current.Name }) -join ' '
      if (Is-Excluded $a.name $titles) { return @{ code = 'app_excluded' } }
      return $a
    }
  }
  return @{ code = 'no_such_app' }
}
function Win-Info($w) { $i = Node-Info $w; @{ index = 0; title = $i.name; role = $i.role; active = $false; showing = $i.showing; modal = $false; bounds = $null } }

function Do-State($req) {
  $a = Find-App $req.app
  if ($a.code) { return Bad $a.code }
  $wins = @($a.windows)
  $idx = 0; if ($req.window_index -ne $null) { $idx = [int]$req.window_index }
  if ($idx -ge $wins.Count) { return Bad 'no_such_window' }
  $win = $wins[$idx]
  $sid = ([guid]::NewGuid().ToString('N')).Substring(0, 16)
  $sigs = @{}; $recs = New-Object System.Collections.ArrayList
  $stack = New-Object System.Collections.Stack
  $stack.Push(@{ e = $win; path = @(); parent = -1 })
  $cutNodes = $false; $cutDepth = $false; $script:hiddenCount = 0
  while ($stack.Count -gt 0) {
    $it = $stack.Pop()
    if ($recs.Count -ge $MAX_NODES) { $cutNodes = $true; break }
    $info = Node-Info $it.e
    $me = $recs.Count
    [void]$recs.Add(@{ info = $info; path = $it.path; kids = New-Object System.Collections.ArrayList })
    if ($it.parent -ge 0) { [void]$recs[$it.parent].kids.Add($me) }
    $sigs[($it.path -join '.')] = Sig $info
    $children = @(); $c = $Walker.GetFirstChild($it.e)
    while ($c -ne $null) { $children += , $c; $c = $Walker.GetNextSibling($c) }
    if ($it.path.Count -ge $MAX_DEPTH) { if ($children.Count -gt 0) { $cutDepth = $true } }
    else { for ($n = $children.Count - 1; $n -ge 0; $n--) { $stack.Push(@{ e = $children[$n]; path = ($it.path + $n); parent = $me }) } }
  }
  function Build($i) {
    $r = $recs[$i]; $inf = $r.info
    $node = @{ id = $(if ($r.path.Count -eq 0) { 'root' } else { $r.path -join '.' }); role = $inf.role }
    if ($inf.name) { $node.name = $inf.name }
    if ($inf.actions.Count -gt 0) { $node.actions = $inf.actions }
    if ($inf.value -ne $null) {
      if (Hide-Value $inf) { $node.value = '[hidden]'; $node.hidden = $true; $script:hiddenCount++ }
      else { $node.value = "$($inf.value)".Substring(0, [Math]::Min($MAX_VALUE, "$($inf.value)".Length)) }
    }
    if ($inf.editable) { $node.editable = $true }
    $kids = @(); foreach ($k in $r.kids) { $kids += , (Build $k) }
    if ($kids.Count -gt 0) { $node.children = $kids }
    return $node
  }
  $tree = Build 0
  $Snaps[$sid] = @{ pid = $a.pid; start = (Proc-Start $a.pid); name = $a.name; window = $idx; title = (Node-Info $win).name; sigs = $sigs; made = Get-Date }
  $tgt = @{ version = 1; backend = 'uia/1'; targetKind = 'window'; stableTargetHandle = "uia:$($a.pid):$($Snaps[$sid].start):$idx"; processStartIdentity = "$($a.pid):$($Snaps[$sid].start)"; targetGeneration = $sid; focusState = 'showing'; permission = 'accessibility'; actionCapabilities = @('click', 'set_value', 'type', 'focus') }
  Out-Line @{ ok = $true; snapshot_id = $sid; target = $tgt; app = @{ name = $a.name; pid = $a.pid }; window = @{ index = $idx; title = $Snaps[$sid].title; showing = $true; active = $false; windows = $wins.Count; modal = $false; modal_open = $false; blocked_by_modal = $false; occlusion = 'unknown' }
    coordinate_space = 'window'; tree = $tree; truncated = @{ nodes = $cutNodes; depth = $cutDepth }; counts = @{ nodes = $recs.Count; hidden = $script:hiddenCount }
    limits = @{ max_nodes = $MAX_NODES; max_depth = $MAX_DEPTH; deepest_seen = 0 }; controls = @{ unnamed_actionable = 0; missing = 'unknown: the tree lists only what the application exposes' } }
}
function Do-Act($req) {
  $s = $Snaps["$($req.snapshot_id)"]
  if ($s -eq $null -or ((Get-Date) - $s.made).TotalSeconds -gt $TTL) { return Bad 'snapshot_expired' }
  $path = @(); if ("$($req.element)" -ne 'root') { $path = @("$($req.element)".Split('.') | ForEach-Object { [int]$_ }) }
  $app = $null
  foreach ($a in (Apps).Values) { if ($a.pid -eq $s.pid -and (Proc-Start $a.pid) -eq $s.start) { $app = $a } }
  if ($app -eq $null) { return Bad 'target_gone' }
  if (Is-Excluded $app.name '') { return Bad 'app_excluded' }
  $wins = @($app.windows)
  if ($s.window -ge $wins.Count -or (Node-Info $wins[$s.window]).name -ne $s.title) { return Bad 'window_changed' }
  $saved = $s.sigs[($path -join '.')]
  if ($saved -eq $null) { return Bad 'no_such_element' }
  $cur = $wins[$s.window]
  foreach ($n in $path) {
    $c = $Walker.GetFirstChild($cur); $k = 0
    while ($c -ne $null -and $k -lt $n) { $c = $Walker.GetNextSibling($c); $k++ }
    if ($c -eq $null) { return Bad 'stale_element' }
    $cur = $c
  }
  $info = Node-Info $cur
  if ((Sig $info) -ne $saved) { return Bad 'stale_element' }
  $verified = 'unknown'
  switch ($req.action) {
    'click' {
      $p = $null
      if ($cur.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke() }
      elseif ($cur.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle() }
      elseif ($cur.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select() }
      else { return Bad 'not_actionable' }
    }
    'secondary' {
      $want = "$($req.name)".ToLower(); $done = $false
      $p = $null
      if ($want -eq 'invoke' -and $cur.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, [ref]$p)) { $p.Invoke(); $done = $true }
      elseif ($want -eq 'toggle' -and $cur.TryGetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern, [ref]$p)) { $p.Toggle(); $done = $true }
      elseif ($want -eq 'select' -and $cur.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$p)) { $p.Select(); $done = $true }
      if (-not $done) { return Bad 'not_actionable' }
    }
    'set_value' {
      $p = $null
      if (-not $cur.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p) -or $p.Current.IsReadOnly) { return Bad 'not_actionable' }
      $p.SetValue("$($req.text)")
      $now = Get-Value $cur; if ($now -ne $null) { $verified = ($now.text -eq "$($req.text)") }
    }
    'type' {
      # By element, without key injection: append through the Value pattern. Elements without one are not_actionable.
      $p = $null
      if (-not $cur.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$p) -or $p.Current.IsReadOnly) { return Bad 'not_actionable' }
      $before = "$($p.Current.Value)"; $p.SetValue($before + "$($req.text)")
      $now = Get-Value $cur; if ($now -ne $null) { $verified = ($now.text -eq ($before + "$($req.text)")) }
    }
    'focus' { $cur.SetFocus() }
    default { return Bad 'unsupported_action' }
  }
  $after = @((Apps).Values | Where-Object { $_.pid -eq $s.pid })
  $changed = ($after.Count -eq 0) -or ((Node-Info @($after[0].windows)[$s.window]).name -ne $s.title)
  Out-Line @{ ok = $true; outcome = 'performed'; verified = $verified; window_changed = $changed; target_before = @{ version = 1 }; target_after = @{ version = 1 } }
}
function Run-A11y($req) {
  switch ($req.op) {
    'handshake' { Out-Line @{ ok = $true; backend = 'uia/1'; accessibility = 'ready'; actions = @('click', 'secondary', 'set_value', 'type', 'focus'); limits = @{ nodes = $MAX_NODES; depth = $MAX_DEPTH; value_chars = $MAX_VALUE } } }
    'list_apps' {
      $list = @()
      foreach ($a in (Apps).Values) {
        $titles = ($a.windows | ForEach-Object { $_.Current.Name }) -join ' '
        if (Is-Excluded $a.name $titles) { continue }
        $list += , @{ name = $a.name; pid = $a.pid; toolkit = 'uia'; windows = @($a.windows).Count }
      }
      Out-Line @{ ok = $true; apps = $list }
    }
    'list_windows' {
      $a = Find-App $req.app
      if ($a.code) { Bad $a.code } else {
        $i = 0; $ws = @(); foreach ($w in $a.windows) { $inf = Win-Info $w; $inf.index = $i; $ws += , $inf; $i++ }
        Out-Line @{ ok = $true; windows = $ws }
      }
    }
    'app_state' { Do-State $req }
    'act' { Do-Act $req }
    default { Bad 'invalid_request' }
  }
}

# ---- pointer and keys (control mode): keysyms from the shared protocol map to virtual keys
function Sym-To-Vk($sym) {
  $map = @{ 0xff0d = 0x0D; 0xff09 = 0x09; 0xff1b = 0x1B; 0xff08 = 0x08; 0xffff = 0x2E; 0xff51 = 0x25; 0xff52 = 0x26; 0xff53 = 0x27; 0xff54 = 0x28; 0xff50 = 0x24; 0xff57 = 0x23; 0xff55 = 0x21; 0xff56 = 0x22; 0xffe3 = 0x11; 0xffe1 = 0x10; 0xffe9 = 0x12 }
  if ($map.ContainsKey([int]$sym)) { return [uint16]$map[[int]$sym] }
  if ($sym -ge 0xffbe -and $sym -le 0xffc9) { return [uint16](0x70 + ($sym - 0xffbe)) }
  if ($sym -ge 0x61 -and $sym -le 0x7a) { return [uint16]($sym - 32) }
  if ($sym -ge 0x30 -and $sym -le 0x39) { return [uint16]$sym }
  if ($sym -eq 0x20) { return [uint16]0x20 }
  return 0
}
function Release-All() {
  for ($i = $Held.Count - 1; $i -ge 0; $i--) {
    $h = $Held[$i]
    $ok = if ($h.kind -eq 'key') { [OcInput]::Key($h.vk, $true) } elseif ($h.kind -eq 'char') { [OcInput]::Char($h.char, $true) } else { [OcInput]::Button($h.flags) }
    if ($ok) { $Held.RemoveAt($i) }
  }
  return $Held.Count -eq 0
}
function Require-Input($ok) { if (-not $ok) { throw 'input_outcome_unknown' } }

function Run-Control($req, [ref]$state) {
  switch ($req.op) {
    'begin' { $b = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds; $state.Value = @{ w = $b.Width; h = $b.Height; x = $b.X; y = $b.Y }; Out-Line @{ ok = $true; state = 'active'; stream = @{ width = $b.Width; height = $b.Height } } }
    'click' {
      $s = $state.Value
      if ($req.frame.width -ne $s.w -or $req.frame.height -ne $s.h) { return Bad 'frame_mismatch' }
      if ($req.x -lt 0 -or $req.y -lt 0 -or $req.x -ge $s.w -or $req.y -ge $s.h) { return Bad 'out_of_bounds' }
      Require-Input ([OcInput]::SetCursorPos($s.x + [int]$req.x, $s.y + [int]$req.y))
      $down, $up = switch ($req.button) { 'right' { 0x8, 0x10 } 'middle' { 0x20, 0x40 } default { 0x2, 0x4 } }
      [void]$Held.Add(@{ kind = 'btn'; flags = $up }); Require-Input ([OcInput]::Button($down)); Require-Input ([OcInput]::Button($up)); $Held.RemoveAt($Held.Count - 1)
      Out-Line @{ ok = $true; outcome = 'sent'; actions_left = 19 }
    }
    'key' {
      $vks = @(); foreach ($k in $req.keysyms) { $v = Sym-To-Vk $k; if ($v -eq 0) { return Bad 'invalid_request' }; $vks += $v }
      foreach ($v in $vks) { [void]$Held.Add(@{ kind = 'key'; vk = $v }); Require-Input ([OcInput]::Key($v, $false)) }
      for ($i = $vks.Count - 1; $i -ge 0; $i--) { Require-Input ([OcInput]::Key($vks[$i], $true)); $Held.RemoveAt($Held.Count - 1) }
      Out-Line @{ ok = $true; outcome = 'sent'; actions_left = 19 }
    }
    'type' {
      foreach ($c in "$($req.text)".ToCharArray()) { [void]$Held.Add(@{ kind = 'char'; char = $c }); Require-Input ([OcInput]::Char($c, $false)); Require-Input ([OcInput]::Char($c, $true)); $Held.RemoveAt($Held.Count - 1) }
      Out-Line @{ ok = $true; outcome = 'sent'; actions_left = 19 }
    }
    'stop' { if (Release-All) { Out-Line @{ ok = $true; state = 'stopped' } } else { Bad 'outcome_unknown' }; exit 0 }
    default { Bad 'unsupported_action' }     # move, scroll, drag, paste, probe: not in the Windows minimum
  }
}

# Host-created local pipe proves this process consumed the private launch bootstrap.
try {
  $bootstrap = [Console]::In.ReadLine() | ConvertFrom-Json
  if ($bootstrap.v -ne 1 -or $bootstrap.op -ne 'launch_proof' -or
      $bootstrap.pipe -cnotmatch '^openclank-control-[0-9a-f]{48}$' -or
      $bootstrap.secret -cnotmatch '^[0-9a-f]{64}$') { exit 1 }
  $pipe = [System.IO.Pipes.NamedPipeClientStream]::new('.', $bootstrap.pipe, [System.IO.Pipes.PipeDirection]::InOut)
  try {
    $pipe.Connect(5000)
    $bytes = [System.Text.Encoding]::ASCII.GetBytes($bootstrap.secret + "`n")
    $pipe.Write($bytes, 0, $bytes.Length); $pipe.Flush()
    if ($pipe.ReadByte() -ne 49) { throw 'launch proof refused' }
  } finally { $pipe.Dispose() }
  $bootstrap = $null; $bytes = $null
  Out-Line @{ ok = $true; state = 'launch_proof_sent' }
} catch { exit 1 }

$ctl = $null
try {
  while ($true) {
    $line = [Console]::In.ReadLine()
    if ($line -eq $null) { break }
    try { $req = $line | ConvertFrom-Json } catch { Bad 'invalid_request'; continue }
    if ($req.v -ne 1) { Bad 'invalid_request'; continue }
    try {
      if ($Mode -eq 'a11y') { Run-A11y $req } else { Run-Control $req ([ref]$ctl) }
    } catch { if ($Mode -eq 'control') { [void](Release-All); Bad 'outcome_unknown'; break } else { Bad 'action_failed' } }
  }
} finally { [void](Release-All) }

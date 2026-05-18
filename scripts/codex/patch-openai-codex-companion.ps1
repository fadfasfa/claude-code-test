[CmdletBinding(SupportsShouldProcess = $true)]
param(
  [string]$PluginRoot = '',
  [string]$PluginCacheRoot = '',
  [switch]$CheckOnly,
  [switch]$RestoreLatestBackup,
  [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$patchId = 'cc-cx-researcher-runtime-v1'
$companionRelativePath = 'scripts\codex-companion.mjs'
$codexRelativePath = 'scripts\lib\codex.mjs'

function Normalize-JsText {
  param([Parameter(Mandatory = $true)][string]$Text)
  return ($Text -replace "`r`n", "`n")
}

function Read-JsText {
  param([Parameter(Mandatory = $true)][string]$Path)
  return [System.IO.File]::ReadAllText($Path)
}

function Write-JsText {
  param(
    [Parameter(Mandatory = $true)][string]$Path,
    [Parameter(Mandatory = $true)][string]$OriginalText,
    [Parameter(Mandatory = $true)][string]$NormalizedText
  )

  $nextText = if ($OriginalText.Contains("`r`n")) {
    $NormalizedText -replace "`n", "`r`n"
  } else {
    $NormalizedText
  }

  $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
  [System.IO.File]::WriteAllText($Path, $nextText, $utf8NoBom)
}

function Get-UniqueExistingPaths {
  param([string[]]$Paths)

  $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  foreach ($path in $Paths) {
    if ([string]::IsNullOrWhiteSpace($path)) {
      continue
    }

    if (-not (Test-Path -LiteralPath $path)) {
      continue
    }

    $resolved = (Resolve-Path -LiteralPath $path).Path
    if ($seen.Add($resolved)) {
      $resolved
    }
  }
}

function Get-DefaultPluginCacheRoots {
  $roots = @()

  if (-not [string]::IsNullOrWhiteSpace($PluginCacheRoot)) {
    $roots += $PluginCacheRoot
  }

  if (-not [string]::IsNullOrWhiteSpace($env:CLAUDE_HOME)) {
    $roots += (Join-Path $env:CLAUDE_HOME 'plugins\cache\openai-codex')
  }

  if (-not [string]::IsNullOrWhiteSpace($env:USERPROFILE)) {
    $roots += (Join-Path $env:USERPROFILE '.claude\plugins\cache\openai-codex')
  }

  if (-not [string]::IsNullOrWhiteSpace($HOME)) {
    $roots += (Join-Path $HOME '.claude\plugins\cache\openai-codex')
  }

  return @(Get-UniqueExistingPaths -Paths $roots)
}

function New-PluginTarget {
  param([Parameter(Mandatory = $true)][string]$Root)

  $companionPath = Join-Path $Root $companionRelativePath
  $codexPath = Join-Path $Root $codexRelativePath

  if ((Test-Path -LiteralPath $companionPath) -and (Test-Path -LiteralPath $codexPath)) {
    return [pscustomobject]@{
      PluginRoot = (Resolve-Path -LiteralPath $Root).Path
      CompanionPath = (Resolve-Path -LiteralPath $companionPath).Path
      CodexPath = (Resolve-Path -LiteralPath $codexPath).Path
    }
  }

  return $null
}

function Find-PluginTargets {
  if (-not [string]::IsNullOrWhiteSpace($PluginRoot)) {
    $target = New-PluginTarget -Root $PluginRoot
    if ($null -eq $target) {
      throw ("PluginRoot does not contain {0} and {1}: {2}" -f $companionRelativePath, $codexRelativePath, $PluginRoot)
    }
    return @($target)
  }

  $targets = @()
  foreach ($root in Get-DefaultPluginCacheRoots) {
    $rootTarget = New-PluginTarget -Root $root
    if ($null -ne $rootTarget) {
      $targets += $rootTarget
      continue
    }

    $companionFiles = @(Get-ChildItem -LiteralPath $root -Recurse -Filter 'codex-companion.mjs' -File -ErrorAction Stop)
    foreach ($file in $companionFiles) {
      if ($file.FullName -notmatch '\\plugins\\cache\\openai-codex\\') {
        continue
      }

      $candidateRoot = Split-Path -Parent (Split-Path -Parent $file.FullName)
      $target = New-PluginTarget -Root $candidateRoot
      if ($null -ne $target) {
        $targets += $target
      }
    }
  }

  $seen = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
  return @($targets | Where-Object { $seen.Add($_.PluginRoot) })
}

function Test-RuntimeCapabilities {
  param(
    [Parameter(Mandatory = $true)][string]$CompanionText,
    [Parameter(Mandatory = $true)][string]$CodexText
  )

  $checks = @(
    [pscustomobject]@{
      Name = 'delegation_metadata_injection'
      Present = (
        $CompanionText -match 'function buildTaskDelegationContext' -and
        $CompanionText -match 'CODEX_DELEGATION_SOURCE' -and
        $CompanionText -match 'CODEX_DELEGATION_ROLE' -and
        $CompanionText -match 'CODEX_DELEGATION_PHASE' -and
        $CompanionText -match 'source: "codex-thread"'
      )
    },
    [pscustomobject]@{
      Name = 'delegated_task_direct_app_server'
      Present = (
        $CompanionText -match 'disableBroker:\s*true' -and
        $CodexText -match 'function withAppServer\(cwd, fn, options = \{\}\)' -and
        $CodexText -match 'const env = options\.env \?\? process\.env' -and
        $CodexText -match 'disableBroker = Boolean\(options\.disableBroker\)' -and
        $CodexText -match 'CodexAppServerClient\.connect\(cwd, \{\s*env,\s*disableBroker' -and
        $CodexText -match 'disableBroker:\s*options\.disableBroker'
      )
    },
    [pscustomobject]@{
      Name = 'researcher_task_local_codex_home'
      Present = (
        $CompanionText -match 'DELEGATED_RESEARCHER_RUNTIME_ROOT' -and
        $CompanionText -match 'ensureDelegatedResearcherRuntimeHome' -and
        $CompanionText -match 'CODEX_HOME:\s*runtimeHome' -and
        $CompanionText -match 'CODEX_COMPANION_CODEX_EXECUTABLE'
      )
    },
    [pscustomobject]@{
      Name = 'researcher_readonly_rules_allowlist'
      Present = (
        $CompanionText -match 'buildDelegatedResearcherRules' -and
        $CompanionText -match '"cmd /c type"' -and
        $CompanionText -match '"cmd /c findstr"' -and
        $CompanionText -match '"rg"' -and
        $CompanionText -match '"git ls-files"' -and
        $CompanionText -match '\[\["cmd\.exe", "cmd"\], "/c", "type"\]' -and
        $CompanionText -match '\[\["cmd\.exe", "cmd"\], "/c", "findstr"\]' -and
        $CompanionText -match '\["git", \["status", "diff", "log", "ls-files"\]\]'
      )
    },
    [pscustomobject]@{
      Name = 'researcher_run_write_still_forbidden'
      Present = (
        $CompanionText -match 'Do not write files' -and
        $CompanionText -match 'repository guard still enforces protected-path and write restrictions' -and
        $CompanionText -notmatch 'pattern = \["Bash\(\*\)"\]' -and
        $CompanionText -notmatch '(?m)^\s*pattern = .*?(writeFileSync|Set-Content|Out-File|Add-Content|tee|git rm|Remove-Item)'
      )
    }
  )

  return $checks
}

function Replace-ExactOnce {
  param(
    [Parameter(Mandatory = $true)][string]$Text,
    [Parameter(Mandatory = $true)][string]$OldText,
    [Parameter(Mandatory = $true)][string]$NewText,
    [Parameter(Mandatory = $true)][string]$Label
  )

  $count = [regex]::Matches($Text, [regex]::Escape($OldText)).Count
  if ($count -ne 1) {
    throw "Patch context mismatch for $Label; expected exactly 1 match, found $count. Stop without guessing."
  }

  return $Text.Replace($OldText, $NewText)
}

function New-PatchedCompanionText {
  param([Parameter(Mandatory = $true)][string]$Text)

  $next = $Text

  $next = Replace-ExactOnce -Text $next -Label 'companion imports' -OldText @'
import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
'@ -NewText @'
import { execFileSync, spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
'@

  $next = Replace-ExactOnce -Text $next -Label 'companion delegation constants' -OldText @'
const STOP_REVIEW_TASK_MARKER = "Run a stop-gate review of the previous Claude turn.";

'@ -NewText @'
const STOP_REVIEW_TASK_MARKER = "Run a stop-gate review of the previous Claude turn.";
const DEFAULT_EXEC_CODEX_HOME = "C:\\Users\\apple\\.codex-exec";
const DEFAULT_REAL_CODEX_EXECUTABLE = "C:\\Users\\apple\\AppData\\Local\\OpenAI\\Codex\\bin\\codex.exe";
const DELEGATED_RESEARCHER_RUNTIME_ROOT = path.join(os.tmpdir(), "codex-delegation-runtime");
const DELEGATED_RESEARCHER_RULE_PREFIXES = [
  "Get-Content",
  "type",
  "cmd /c type",
  "cmd /c findstr",
  "findstr",
  "Select-String",
  "rg",
  "grep",
  "Get-ChildItem",
  "dir",
  "ls",
  "git status",
  "git diff",
  "git log",
  "git ls-files"
];

'@

  $next = Replace-ExactOnce -Text $next -Label 'companion task run app-server call' -OldText @'
  const result = await runAppServerTurn(workspaceRoot, {
    resumeThreadId,
    prompt: request.prompt,
    defaultPrompt: resumeThreadId ? DEFAULT_CONTINUE_PROMPT : "",
    model: request.model,
    effort: request.effort,
    sandbox: request.write ? "workspace-write" : "read-only",
    onProgress: request.onProgress,
    persistThread: true,
    threadName: resumeThreadId ? null : buildPersistentTaskThreadName(request.prompt || DEFAULT_CONTINUE_PROMPT)
  });
'@ -NewText @'
  const delegation = buildTaskDelegationContext(request);
  const taskPrompt = buildDelegatedTaskPrompt(request.prompt, delegation);
  const result = await runAppServerTurn(workspaceRoot, {
    resumeThreadId,
    prompt: taskPrompt,
    defaultPrompt: resumeThreadId ? DEFAULT_CONTINUE_PROMPT : "",
    model: request.model,
    effort: request.effort,
    sandbox: request.write ? "workspace-write" : "read-only",
    env: buildTaskRuntimeEnv(delegation),
    disableBroker: true,
    onProgress: request.onProgress,
    persistThread: true,
    threadName: resumeThreadId ? null : buildPersistentTaskThreadName(request.prompt || DEFAULT_CONTINUE_PROMPT)
  });
'@

  $next = Replace-ExactOnce -Text $next -Label 'companion task payload delegation' -OldText @'
    rawOutput,
    touchedFiles: result.touchedFiles,
    reasoningSummary: result.reasoningSummary
  };
'@ -NewText @'
    rawOutput,
    touchedFiles: result.touchedFiles,
    reasoningSummary: result.reasoningSummary,
    delegation
  };
'@

  $next = Replace-ExactOnce -Text $next -Label 'companion delegation helpers' -OldText @'
function renderQueuedTaskLaunch(payload) {
'@ -NewText @'
function buildTaskDelegationContext({ write = false, jobId = null } = {}) {
  return {
    source: "codex-thread",
    role: write ? "executor" : "researcher",
    phase: write ? "execute" : "explore",
    jobId: jobId == null ? "" : String(jobId)
  };
}

function buildTaskRuntimeEnv(delegation) {
  const runtimeHome = delegation.role === "researcher" && delegation.phase === "explore"
    ? ensureDelegatedResearcherRuntimeHome()
    : null;

  return {
    ...process.env,
    ...(runtimeHome
      ? {
          CODEX_HOME: runtimeHome,
          CODEX_COMPANION_CODEX_EXECUTABLE: DEFAULT_REAL_CODEX_EXECUTABLE
        }
      : {}),
    CODEX_DELEGATION_SOURCE: delegation.source,
    CODEX_DELEGATION_ROLE: delegation.role,
    CODEX_DELEGATION_PHASE: delegation.phase,
    CODEX_DELEGATION_JOB_ID: delegation.jobId
  };
}

function ensureDelegatedResearcherRuntimeHome() {
  const runtimeHome = path.join(DELEGATED_RESEARCHER_RUNTIME_ROOT, "researcher-explore");
  const rulesDir = path.join(runtimeHome, "rules");
  fs.mkdirSync(rulesDir, { recursive: true });

  const sourceHome = process.env.CODEX_HOME && process.env.CODEX_HOME.trim() ? process.env.CODEX_HOME.trim() : DEFAULT_EXEC_CODEX_HOME;
  const sourceConfigPath = path.join(sourceHome, "config.toml");
  const sourceRulesPath = path.join(sourceHome, "rules", "default.rules");
  const runtimeConfigPath = path.join(runtimeHome, "config.toml");
  const runtimeRulesPath = path.join(rulesDir, "default.rules");

  if (fs.existsSync(sourceConfigPath)) {
    fs.copyFileSync(sourceConfigPath, runtimeConfigPath);
  }

  const baseRules = fs.existsSync(sourceRulesPath) ? fs.readFileSync(sourceRulesPath, "utf8").trimEnd() : "";
  const delegatedRules = buildDelegatedResearcherRules();
  const nextRulesContent = baseRules ? `${baseRules}\n\n${delegatedRules}\n` : `${delegatedRules}\n`;
  fs.writeFileSync(runtimeRulesPath, nextRulesContent, "utf8");

  return runtimeHome;
}

function buildDelegatedResearcherRules() {
  const shellPaths = resolvePwshRuleCandidates();
  const shellArray = formatRulesArray(shellPaths);
  const prefixArray = formatRulesArray(DELEGATED_RESEARCHER_RULE_PREFIXES);
  const justification = "Allow delegated Codex Researcher read-only inspection commands; repository guard still enforces protected-path and write restrictions.";

  return [
    "prefix_rule(",
    `    pattern = [["Get-Content", "type", "findstr", "Select-String", "rg", "grep", "Get-ChildItem", "dir", "ls"]],`,
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")",
    "",
    "prefix_rule(",
    '    pattern = [["cmd.exe", "cmd"], "/c", "type"],',
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")",
    "",
    "prefix_rule(",
    '    pattern = [["cmd.exe", "cmd"], "/c", "findstr"],',
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")",
    "",
    "prefix_rule(",
    '    pattern = ["git", ["status", "diff", "log", "ls-files"]],',
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")",
    "",
    "prefix_rule(",
    `    pattern = [${shellArray}, "-Command", ${prefixArray}],`,
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")",
    "",
    "prefix_rule(",
    `    pattern = [${shellArray}, "-NoProfile", "-Command", ${prefixArray}],`,
    '    decision = "allow",',
    `    justification = ${JSON.stringify(justification)},`,
    ")"
  ].join("\n");
}

function resolvePwshRuleCandidates() {
  const candidates = new Set([
    "C:\\Program Files\\WindowsApps\\Microsoft.PowerShell_7.6.1.0_x64__8wekyb3d8bbwe\\pwsh.exe",
    "C:\\Users\\apple\\AppData\\Local\\Microsoft\\WindowsApps\\pwsh.exe"
  ]);

  try {
    const stdout = execFileSync("where.exe", ["pwsh"], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"]
    });
    for (const line of stdout.split(/\r?\n/)) {
      const candidate = line.trim();
      if (candidate) {
        candidates.add(candidate);
      }
    }
  } catch {
    // Fall back to the common Windows PowerShell shims above.
  }

  return [...candidates];
}

function formatRulesArray(values) {
  return `[${values.map((value) => JSON.stringify(String(value))).join(", ")}]`;
}

function buildDelegatedTaskPrompt(prompt, delegation) {
  const userPrompt = String(prompt ?? "").trim();
  if (delegation.role !== "researcher" || delegation.phase !== "explore") {
    return userPrompt;
  }

  const header = [
    "Delegation context:",
    `- source=${delegation.source}`,
    `- role=${delegation.role}`,
    `- phase=${delegation.phase}`,
    `- job_id=${delegation.jobId || "n/a"}`,
    "Execution policy for this task:",
    "- You are a Codex Researcher running inside a CC delegated task.",
    "- Read-only exploration of protected paths is authorized for allowlisted inspection commands.",
    "- Allowed inspection commands include Get-Content, type, cmd /c type, cmd /c findstr, findstr, Select-String, rg, grep, Get-ChildItem, dir, ls, git status, git diff, git log, and git ls-files.",
    "- Do not write files, do not run destructive git commands, and do not exceed the user's explicit command list.",
    ""
  ].join("\n");

  return userPrompt ? `${header}\n${userPrompt}` : header;
}

function renderQueuedTaskLaunch(payload) {
'@

  return $next
}

function New-PatchedCodexText {
  param([Parameter(Mandatory = $true)][string]$Text)

  $next = $Text

  $next = Replace-ExactOnce -Text $next -Label 'codex withAppServer options' -OldText @'
async function withAppServer(cwd, fn) {
  let client = null;
  try {
    client = await CodexAppServerClient.connect(cwd);
    const result = await fn(client);
    await client.close();
    return result;
  } catch (error) {
    const brokerRequested = client?.transport === "broker" || Boolean(process.env[BROKER_ENDPOINT_ENV]);
    const shouldRetryDirect =
      (client?.transport === "broker" && error?.rpcCode === BROKER_BUSY_RPC_CODE) ||
      (brokerRequested && (error?.code === "ENOENT" || error?.code === "ECONNREFUSED"));

    if (client) {
      await client.close().catch(() => {});
      client = null;
    }

    if (!shouldRetryDirect) {
      throw error;
    }

    const directClient = await CodexAppServerClient.connect(cwd, { disableBroker: true });
    try {
      return await fn(directClient);
    } finally {
      await directClient.close();
    }
  }
}
'@ -NewText @'
async function withAppServer(cwd, fn, options = {}) {
  const env = options.env ?? process.env;
  const disableBroker = Boolean(options.disableBroker);
  let client = null;
  try {
    client = await CodexAppServerClient.connect(cwd, {
      env,
      disableBroker
    });
    const result = await fn(client);
    await client.close();
    return result;
  } catch (error) {
    const brokerRequested = !disableBroker && (client?.transport === "broker" || Boolean(env[BROKER_ENDPOINT_ENV]));
    const shouldRetryDirect =
      (client?.transport === "broker" && error?.rpcCode === BROKER_BUSY_RPC_CODE) ||
      (brokerRequested && (error?.code === "ENOENT" || error?.code === "ECONNREFUSED"));

    if (client) {
      await client.close().catch(() => {});
      client = null;
    }

    if (!shouldRetryDirect) {
      throw error;
    }

    const directClient = await CodexAppServerClient.connect(cwd, {
      env,
      disableBroker: true
    });
    try {
      return await fn(directClient);
    } finally {
      await directClient.close();
    }
  }
}
'@

  $next = Replace-ExactOnce -Text $next -Label 'codex runAppServerTurn open' -OldText @'
export async function runAppServerTurn(cwd, options = {}) {
  const availability = getCodexAvailability(cwd);
  if (!availability.available) {
    throw new Error("Codex CLI is not installed or is missing required runtime support. Install it with `npm install -g @openai/codex`, then rerun `/codex:setup`.");
  }

  return withAppServer(cwd, async (client) => {
'@ -NewText @'
export async function runAppServerTurn(cwd, options = {}) {
  const availability = getCodexAvailability(cwd);
  if (!availability.available) {
    throw new Error("Codex CLI is not installed or is missing required runtime support. Install it with `npm install -g @openai/codex`, then rerun `/codex:setup`.");
  }

  return withAppServer(
    cwd,
    async (client) => {
'@

  $next = Replace-ExactOnce -Text $next -Label 'codex runAppServerTurn close' -OldText @'
      commandExecutions: turnState.commandExecutions
    };
  });
}
'@ -NewText @'
      commandExecutions: turnState.commandExecutions
    };
    },
    {
      env: options.env,
      disableBroker: options.disableBroker
    }
  );
}
'@

  return $next
}

function New-BackupPath {
  param([Parameter(Mandatory = $true)][string]$Path)
  $timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
  return "$Path.before-$patchId-$timestamp.bak"
}

function Backup-RuntimeFiles {
  param([Parameter(Mandatory = $true)]$Target)

  $companionBackup = New-BackupPath -Path $Target.CompanionPath
  $codexBackup = New-BackupPath -Path $Target.CodexPath

  Copy-Item -LiteralPath $Target.CompanionPath -Destination $companionBackup -ErrorAction Stop
  Copy-Item -LiteralPath $Target.CodexPath -Destination $codexBackup -ErrorAction Stop

  return @($companionBackup, $codexBackup)
}

function Restore-LatestRuntimeBackup {
  param([Parameter(Mandatory = $true)]$Target)

  $companionBackup = Get-ChildItem -LiteralPath (Split-Path -Parent $Target.CompanionPath) -Filter "codex-companion.mjs.before-$patchId-*.bak" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
  $codexBackup = Get-ChildItem -LiteralPath (Split-Path -Parent $Target.CodexPath) -Filter "codex.mjs.before-$patchId-*.bak" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1

  if ($null -eq $companionBackup -or $null -eq $codexBackup) {
    throw "No complete backup pair was found for $($Target.PluginRoot)."
  }

  if ($PSCmdlet.ShouldProcess($Target.PluginRoot, 'restore latest Codex runtime backup')) {
    Copy-Item -LiteralPath $companionBackup.FullName -Destination $Target.CompanionPath -Force -ErrorAction Stop
    Copy-Item -LiteralPath $codexBackup.FullName -Destination $Target.CodexPath -Force -ErrorAction Stop
  }

  return [pscustomobject]@{
    PluginRoot = $Target.PluginRoot
    Status = 'restored'
    RestoredFrom = @($companionBackup.FullName, $codexBackup.FullName)
  }
}

function Invoke-RuntimePatch {
  param([Parameter(Mandatory = $true)]$Target)

  $companionRaw = Read-JsText -Path $Target.CompanionPath
  $codexRaw = Read-JsText -Path $Target.CodexPath
  $companionText = Normalize-JsText -Text $companionRaw
  $codexText = Normalize-JsText -Text $codexRaw
  $beforeChecks = @(Test-RuntimeCapabilities -CompanionText $companionText -CodexText $codexText)
  $missingBefore = @($beforeChecks | Where-Object { -not $_.Present })

  if ($missingBefore.Count -eq 0) {
    return [pscustomobject]@{
      PluginRoot = $Target.PluginRoot
      Status = 'already-patched'
      Changed = $false
      Backups = @()
      Capabilities = $beforeChecks
    }
  }

  if ($CheckOnly) {
    return [pscustomobject]@{
      PluginRoot = $Target.PluginRoot
      Status = 'missing-patch'
      Changed = $false
      Backups = @()
      Capabilities = $beforeChecks
    }
  }

  $patchedCompanionText = New-PatchedCompanionText -Text $companionText
  $patchedCodexText = New-PatchedCodexText -Text $codexText
  $afterChecks = @(Test-RuntimeCapabilities -CompanionText $patchedCompanionText -CodexText $patchedCodexText)
  $missingAfter = @($afterChecks | Where-Object { -not $_.Present })

  if ($missingAfter.Count -gt 0) {
    $names = ($missingAfter | ForEach-Object { $_.Name }) -join ', '
    throw "Patch result still misses required capabilities: $names"
  }

  if (-not $PSCmdlet.ShouldProcess($Target.PluginRoot, 'apply OpenAI Codex companion runtime patch')) {
    return [pscustomobject]@{
      PluginRoot = $Target.PluginRoot
      Status = 'would-patch'
      Changed = $false
      Backups = @()
      Capabilities = $afterChecks
    }
  }

  $backups = Backup-RuntimeFiles -Target $Target
  Write-JsText -Path $Target.CompanionPath -OriginalText $companionRaw -NormalizedText $patchedCompanionText
  Write-JsText -Path $Target.CodexPath -OriginalText $codexRaw -NormalizedText $patchedCodexText

  return [pscustomobject]@{
    PluginRoot = $Target.PluginRoot
    Status = 'patched'
    Changed = $true
    Backups = $backups
    Capabilities = $afterChecks
  }
}

$targets = @(Find-PluginTargets)
if ($targets.Count -eq 0) {
  throw 'No openai-codex plugin cache target was found.'
}

if ($targets.Count -gt 1 -and [string]::IsNullOrWhiteSpace($PluginRoot)) {
  $candidateList = ($targets | ForEach-Object { "  - $($_.PluginRoot)" }) -join [Environment]::NewLine
  throw "Multiple openai-codex plugin cache targets were found. Re-run with -PluginRoot for the intended version:$([Environment]::NewLine)$candidateList"
}

$results = if ($RestoreLatestBackup) {
  foreach ($target in $targets) {
    Restore-LatestRuntimeBackup -Target $target
  }
} else {
  foreach ($target in $targets) {
    Invoke-RuntimePatch -Target $target
  }
}

if ($Json) {
  $results | ConvertTo-Json -Depth 8
} else {
  foreach ($result in $results) {
    "PluginRoot: $($result.PluginRoot)"
    "Status: $($result.Status)"
    if ($result.PSObject.Properties.Name -contains 'Changed') {
      "Changed: $($result.Changed)"
    }
    if (($result.PSObject.Properties.Name -contains 'Backups') -and $result.Backups.Count -gt 0) {
      'Backups:'
      $result.Backups | ForEach-Object { "  - $_" }
    }
    if ($result.PSObject.Properties.Name -contains 'RestoredFrom') {
      'RestoredFrom:'
      $result.RestoredFrom | ForEach-Object { "  - $_" }
    }
    if ($result.PSObject.Properties.Name -contains 'Capabilities') {
      'Capabilities:'
      $result.Capabilities | ForEach-Object {
        "  - $($_.Name): $($_.Present)"
      }
    }
  }
}

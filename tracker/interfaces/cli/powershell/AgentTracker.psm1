#requires -Version 5.1
Set-StrictMode -Version Latest

function Resolve-AgentDaemonUrl {
    [CmdletBinding()]
    param(
        [string]$DaemonUrl
    )
    if (-not [string]::IsNullOrWhiteSpace($DaemonUrl)) {
        return $DaemonUrl.TrimEnd('/')
    }
    if (-not [string]::IsNullOrWhiteSpace($env:AGENT_DASHBOARD_URL)) {
        return $env:AGENT_DASHBOARD_URL.TrimEnd('/')
    }
    if (-not [string]::IsNullOrWhiteSpace($env:AGENT_TRACKER_URL)) {
        return $env:AGENT_TRACKER_URL.TrimEnd('/')
    }
    return 'http://127.0.0.1:8080'
}

function ConvertTo-AgentSessionObject {
    param(
        [Parameter(Mandatory = $true)]
        $RawSession,
        [Parameter(Mandatory = $true)]
        [string]$BaseUrl
    )
    $tokens = $RawSession.tokens
    $inTok = if ($null -ne $tokens -and $null -ne $tokens.input_tokens) { [int]$tokens.input_tokens } else { 0 }
    $outTok = if ($null -ne $tokens -and $null -ne $tokens.output_tokens) { [int]$tokens.output_tokens } else { 0 }
    $cacheTok = if ($null -ne $tokens -and $null -ne $tokens.cache_read_tokens) { [int]$tokens.cache_read_tokens } else { 0 }

    [PSCustomObject]@{
        PSTypeName      = 'AgentTracker.Session'
        SessionId       = [string]$RawSession.session_id
        ParentSessionId = $RawSession.parent_session_id
        AgentType       = [string]$RawSession.agent_type
        ModelName       = [string]$RawSession.model_name
        WorkingDir      = [string]$RawSession.working_dir
        StartedAt       = [string]$RawSession.started_at
        EndedAt         = $RawSession.ended_at
        Status          = [string]$RawSession.status
        InputTokens     = $inTok
        OutputTokens    = $outTok
        CacheReadTokens = $cacheTok
        TotalTokens     = ($inTok + $outTok)
        ResetEpoch      = [int]$RawSession.reset_epoch
        DaemonUrl       = $BaseUrl
    }
}

function Get-AgentSession {
    [CmdletBinding()]
    [OutputType([PSCustomObject])]
    param(
        [Parameter(Position = 0)]
        [ValidateSet('INITIALIZING', 'RUNNING', 'WAITING_USER', 'COMPLETED', 'ERROR', '')]
        [string]$Status = '',

        [Parameter()]
        [string]$DaemonUrl = '',

        [Parameter()]
        [int]$TimeoutSec = 5
    )

    $baseUrl = Resolve-AgentDaemonUrl -DaemonUrl $DaemonUrl
    $uri = "$baseUrl/api/sessions"

    $rawSessions = Invoke-RestMethod -Uri $uri -Method Get -TimeoutSec $TimeoutSec -ErrorAction Stop
    foreach ($item in @($rawSessions)) {
        if ($null -eq $item) { continue }
        if (-not [string]::IsNullOrWhiteSpace($Status)) {
            if ([string]$item.status -ine $Status) {
                continue
            }
        }
        ConvertTo-AgentSessionObject -RawSession $item -BaseUrl $baseUrl
    }
}

function Watch-AgentSession {
    [CmdletBinding()]
    [OutputType([PSCustomObject])]
    param(
        [Parameter(Mandatory = $true, Position = 0, ValueFromPipeline = $true, ValueFromPipelineByPropertyName = $true)]
        [Alias('session_id')]
        [ValidateNotNullOrEmpty()]
        [string]$SessionId,

        [Parameter(ValueFromPipelineByPropertyName = $true)]
        [string]$DaemonUrl = '',

        [Parameter()]
        [int]$HistoryLimit = 20,

        [Parameter()]
        [string]$PythonExecutable = 'python3'
    )

    process {
        $baseUrl = Resolve-AgentDaemonUrl -DaemonUrl $DaemonUrl

        & $PythonExecutable -m tracker.interfaces.cli tail $SessionId --url $baseUrl --limit $HistoryLimit --json |
            ForEach-Object {
                if (-not [string]::IsNullOrWhiteSpace($_)) {
                    $ev = $_ | ConvertFrom-Json
                    [PSCustomObject]@{
                        PSTypeName  = 'AgentTracker.Event'
                        SessionId   = [string]$ev.session_id
                        SequenceNum = [int]$ev.sequence_num
                        EventType   = [string]$ev.event_type
                        Payload     = $ev.payload
                    }
                }
            }
        if ($LASTEXITCODE -ne 0) {
            Write-Error "tracker tail exited with error code $LASTEXITCODE"
        }
    }
}

Export-ModuleMember -Function @('Get-AgentSession', 'Watch-AgentSession')

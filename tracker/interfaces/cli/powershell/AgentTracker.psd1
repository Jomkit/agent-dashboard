@{
    RootModule        = 'AgentTracker.psm1'
    ModuleVersion     = '0.1.0'
    GUID              = 'a7c4d3e2-8f1b-4c62-9b25-1d8a5e03f410'
    Author            = 'Agent Dashboard'
    Description       = 'PowerShell cmdlets for inspecting and streaming Agent Dashboard sessions.'
    PowerShellVersion = '5.1'
    FunctionsToExport = @('Get-AgentSession', 'Watch-AgentSession')
    CmdletsToExport   = @()
    VariablesToExport = @()
    AliasesToExport   = @()
}

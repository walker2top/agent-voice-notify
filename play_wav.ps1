param([Parameter(Mandatory = $true)][string]$Wav)
# 记录每次播放的开始/结束/异常，方便排查"进程起来了但没声音"
# 日志与脚本同目录，整个 skill 目录可以整体搬走
$log = Join-Path $PSScriptRoot 'play.log'
function W($m) { "$(Get-Date -Format 'HH:mm:ss.fff') $m" | Out-File -FilePath $log -Append -Encoding utf8 }
W "start $Wav exists=$(Test-Path -LiteralPath $Wav)"
try {
    $p = New-Object System.Media.SoundPlayer $Wav
    $p.Load()
    W "loaded ok"
    $p.PlaySync()
    W "played"
} catch {
    W "ERROR $($_.Exception.GetType().Name): $($_.Exception.Message)"
}

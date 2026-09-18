# attention_please 模型下载脚本(只需跑一次, 之后完全离线)
# 官方模型桶: https://storage.googleapis.com/mediapipe-models/...
# 体积来源: 2026-xx 实测对象列表, 见 mediapipe-windows-cpu-research.md

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$models = Join-Path $root 'models'
New-Item -ItemType Directory -Force -Path $models | Out-Null

$targets = @(
    @{
        Name = 'pose_landmarker_lite.task'
        Url  = 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/1/pose_landmarker_lite.task'
        Size = 5777746
    },
    @{
        Name = 'face_landmarker.task'
        Url  = 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task'
        Size = 3758596
    }
)

foreach ($t in $targets) {
    $dest = Join-Path $models $t.Name
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -eq $t.Size)) {
        Write-Host "[skip] $($t.Name) 已存在且体积正确"
        continue
    }
    Write-Host "[get ] $($t.Name)"
    Invoke-WebRequest -Uri $t.Url -OutFile $dest -UseBasicParsing
    $len = (Get-Item $dest).Length
    if ($len -ne $t.Size) {
        Write-Warning "$($t.Name) 体积 $len != 预期 $($t.Size), 可能下载不完整"
    } else {
        Write-Host "[ok  ] $($t.Name) $len bytes"
    }
}

Get-ChildItem $models | Select-Object Name, Length | Format-Table -AutoSize

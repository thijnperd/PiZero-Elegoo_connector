$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Drawing

$root = "assets/icons"
$sets = @(
  @{ dir = "16"; icons = @("clock","link","firmware","offline","battery","sd","usb","lock") },
  @{ dir = "24"; icons = @("settings","menu","back","up","down","enter","refresh","check","warn","info","power","search","save","cross") },
  @{ dir = "32"; icons = @("boot") }
)

# ---- blank canvases -------------------------------------------------------
foreach ($s in $sets) {
  $d = Join-Path $root $s.dir
  New-Item -ItemType Directory -Force -Path $d | Out-Null
  $size = [int]$s.dir
  foreach ($n in $s.icons) {
    $bmp = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
    $bmp.Save((Join-Path $d "$n.png"), [System.Drawing.Imaging.ImageFormat]::Png)
    $bmp.Dispose()
  }
}

# ---- labelled reference sheet --------------------------------------------
$scale   = 8
$margin  = 40
$colW    = 250
$cols    = 4
$labelH  = 46
$headH   = 56

$sheetW = $margin * 2 + $colW * $cols
$sheetH = $margin * 2 + 40
foreach ($s in $sets) {
  $rows = [math]::Ceiling($s.icons.Count / $cols)
  $sheetH += $headH + $rows * ([int]$s.dir * $scale + $labelH) + 24
}

$sheet = New-Object System.Drawing.Bitmap($sheetW, $sheetH, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$g = [System.Drawing.Graphics]::FromImage($sheet)
$g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::None
$g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAlias

$bg      = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,20,22,27))
$cellBg  = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,26,29,36))
$labelBr = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,225,228,235))
$dimBr   = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,140,147,160))
$headBr  = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(255,110,150,230))

$gridPen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255,42,47,58)), 1
$grid4   = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255,62,70,86)), 1
$safePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(255,200,70,70)), 2

$g.FillRectangle($bg, 0, 0, $sheetW, $sheetH)

$fontHead  = New-Object System.Drawing.Font("Segoe UI", 15, [System.Drawing.FontStyle]::Bold)
$fontLabel = New-Object System.Drawing.Font("Consolas", 11)
$fontNote  = New-Object System.Drawing.Font("Segoe UI", 9)

$y = $margin
foreach ($s in $sets) {
  $size = [int]$s.dir
  $px   = $size * $scale

  $word  = if ($s.icons.Count -eq 1) { "icon" } else { "icons" }
  $title = "{0}x{0}  -  {1} {2}  -  draw 1:1, red ring = 1px keep-clear" -f $size, $s.icons.Count, $word
  $g.DrawString($title, $fontHead, $headBr, $margin, $y)
  $y += $headH

  for ($i = 0; $i -lt $s.icons.Count; $i++) {
    $col = $i % $cols
    $row = [math]::Floor($i / $cols)
    $cx = $margin + $col * $colW + [math]::Floor(($colW - $px) / 2)
    $cy = $y + $row * ($px + $labelH)

    $g.FillRectangle($cellBg, $cx, $cy, $px, $px)

    for ($k = 1; $k -lt $size; $k++) {
      $pen = if ($k % 4 -eq 0) { $grid4 } else { $gridPen }
      $g.DrawLine($pen, $cx + $k * $scale, $cy, $cx + $k * $scale, $cy + $px)
      $g.DrawLine($pen, $cx, $cy + $k * $scale, $cx + $px, $cy + $k * $scale)
    }

    $inset = $scale
    $g.DrawRectangle($safePen, $cx + $inset, $cy + $inset, $px - $inset * 2 - 1, $px - $inset * 2 - 1)

    $isOpt = ($s.dir -eq "32") -or ($s.icons[$i] -in @("battery","sd","usb","lock"))
    $label = if ($isOpt) { $s.icons[$i] + "  (optional)" } else { $s.icons[$i] }
    $brush = if ($isOpt) { $dimBr } else { $labelBr }
    $g.DrawString($label, $fontLabel, $brush, $cx, $cy + $px + 8)
  }

  $rows = [math]::Ceiling($s.icons.Count / $cols)
  $y += $rows * ($px + $labelH) + 24
}

$note = "Reference only. Draw into assets/icons/<size>/<name>.png.  This sheet is 8x view zoom - never scale your art."
$g.DrawString($note, $fontNote, $dimBr, $margin, $sheetH - $margin + 8)

$sheet.Save((Join-Path $root "SHEET.png"), [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose()
$sheet.Dispose()

$written = (Get-ChildItem -Recurse -Filter *.png $root | Measure-Object).Count
Write-Output ("ok: {0} png files under {1}" -f $written, $root)

# 只读取当前焦点的可访问性属性，不点击页面，也不读取输入框正文。
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

$walker = [System.Windows.Automation.TreeWalker]::RawViewWalker
$focused = [System.Windows.Automation.AutomationElement]::FocusedElement
if ($null -eq $focused) { throw '没有可读取的焦点控件。' }

$controls = @()
$labels = @()
$node = $focused
$windowHandle = 0
for ($level = 0; $level -lt 30 -and $null -ne $node; $level++) {
    $current = $node.Current
    if ($current.ControlType -eq [System.Windows.Automation.ControlType]::Window) {
        $windowHandle = $current.NativeWindowHandle
        break
    }
    if ($level -eq 0 -or $current.HasKeyboardFocus -or $current.ControlType -in @(
        [System.Windows.Automation.ControlType]::Edit,
        [System.Windows.Automation.ControlType]::Document
    )) {
        $rect = $current.BoundingRectangle
        $labeledBy = ''
        if ($null -ne $current.LabeledBy) { $labeledBy = $current.LabeledBy.Current.Name }
        $textReadOnly = $null
        $pattern = $null
        if ($node.TryGetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern, [ref]$pattern)) {
            $attribute = $pattern.DocumentRange.GetAttributeValue([System.Windows.Automation.TextPattern]::IsReadOnlyAttribute)
            if ($attribute -is [bool]) { $textReadOnly = $attribute }
        }
        $controls += [pscustomobject]@{
            name = $current.Name
            help = $current.HelpText
            label = $labeledBy
            automation_id = $current.AutomationId
            class_name = $current.ClassName
            control_type = $current.ControlType.ProgrammaticName
            focused = $current.HasKeyboardFocus
            focus_origin = ($level -eq 0)
            text_read_only = $textReadOnly
            rect = @($rect.Left, $rect.Top, $rect.Right, $rect.Bottom)
        }
        # 富文本编辑器的 placeholder 常是控件内部文本，未放在 Name/HelpText。
        # 限制两层、最多 80 个节点，并只采集位于当前编辑器范围内的短提示。
        $children = [System.Collections.Generic.Queue[object]]::new()
        $child = $walker.GetFirstChild($node)
        while ($null -ne $child -and $children.Count -lt 80) {
            $children.Enqueue(@($child, 0))
            $child = $walker.GetNextSibling($child)
        }
        $visited = 0
        while ($children.Count -gt 0 -and $visited -lt 80) {
            $entry = $children.Dequeue()
            $inside = $entry[0]
            $depth = $entry[1]
            $visited++
            $info = $inside.Current
            $r = $info.BoundingRectangle
            if ($current.HasKeyboardFocus -and
                $info.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
                $info.Name.Length -le 80 -and $r.Width -gt 0 -and
                $r.Left -ge $rect.Left - 2 -and $r.Right -le $rect.Right + 2 -and
                $r.Top -ge $rect.Top - 2 -and $r.Bottom -le $rect.Bottom + 2) {
                $labels += $info.Name
            }
            if ($depth -lt 1) {
                $nested = $walker.GetFirstChild($inside)
                while ($null -ne $nested -and $children.Count -lt 80) {
                    $children.Enqueue(@($nested, $depth + 1))
                    $nested = $walker.GetNextSibling($nested)
                }
            }
        }
        # 只收集输入框左侧同一行的短标签，避免其他字段的名称干扰判断。
        $parent = $walker.GetParent($node)
        if ($null -ne $parent) {
            $sibling = $walker.GetFirstChild($parent)
            for ($index = 0; $index -lt 80 -and $null -ne $sibling; $index++) {
                $s = $sibling.Current
                $r = $s.BoundingRectangle
                if ($current.HasKeyboardFocus -and
                    $s.ControlType -eq [System.Windows.Automation.ControlType]::Text -and
                    $s.Name.Length -le 40 -and
                    (($r.Right -le $rect.Left + 16 -and
                      [Math]::Abs(($r.Top + $r.Bottom - $rect.Top - $rect.Bottom) / 2) -le 25) -or
                     ($r.Left -ge $rect.Left - 2 -and $r.Right -le $rect.Right + 2 -and
                      $r.Top -ge $rect.Top - 2 -and $r.Bottom -le $rect.Bottom + 2))) {
                    $labels += $s.Name
                }
                $sibling = $walker.GetNextSibling($sibling)
            }
        }
    }
    $node = $walker.GetParent($node)
}

[pscustomobject]@{
    controls = @($controls)
    nearby_labels = @($labels)
    window_handle = $windowHandle
} | ConvertTo-Json -Depth 5 -Compress

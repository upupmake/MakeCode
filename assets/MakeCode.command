#!/bin/zsh

SCRIPT_PATH="${0:A}"
SCRIPT_DIR="${SCRIPT_PATH:h}"
APP_DIR="$SCRIPT_DIR/MakeCode"

# Inspect/remove attributes on symlinks themselves, never on external targets.
if ! attributes=$(/usr/bin/xattr -rs "$SCRIPT_PATH" "$APP_DIR"); then
    print -u2 -r -- "无法检查发布包的下载隔离状态，未启动 MakeCode。"
    exit 1
fi

if print -r -- "$attributes" | /usr/bin/grep -q ': com\.apple\.quarantine$'; then
    print -r -- "检测到 macOS 下载隔离。仅在确认发布包来源可信时继续。"
    print -r -- "将仅解除以下启动器和应用目录（含内部文件）的下载隔离："
    print -r -- "  $SCRIPT_PATH"
    print -r -- "  $APP_DIR"
    print -r -- "不会关闭系统安全检查，也不会授予管理员权限。"

    if [[ ! -t 0 ]]; then
        print -u2 -r -- "需要在终端中交互确认。未修改隔离属性，未启动 MakeCode。"
        exit 1
    fi

    if ! read -r "answer?确认信任并解除下载隔离？[y/N]: " || [[ "${answer:l}" != y && "${answer:l}" != yes ]]; then
        print -r -- "已取消。未修改隔离属性，未启动 MakeCode。"
        exit 1
    fi

    if ! /usr/bin/xattr -drs com.apple.quarantine "$SCRIPT_PATH" "$APP_DIR"; then
        print -u2 -r -- "解除下载隔离失败，未启动 MakeCode。请检查以上路径及错误信息。"
        exit 1
    fi
fi

exec "$APP_DIR/MakeCode" "$@"

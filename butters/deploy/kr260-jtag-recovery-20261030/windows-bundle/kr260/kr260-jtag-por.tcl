# KR260 guarded JTAG power-on reset, run by XSDB on the Windows desktop.
#   xsdb -no-ini kr260-jtag-por.tcl check <cable-serial> <board-address>
#   xsdb -no-ini kr260-jtag-por.tcl por   <cable-serial> <board-address> confirm-por
# `check` attaches, validates the cable and chain, selects the target and
# detaches. `por` does the same, then pulses the cable's POR line once.
#
# Both refuse unless the board is still dark immediately before attaching and
# again, while attached, immediately before CHECK_OK or rst -por: one TCP
# connection attempt to its SSH port, closed without sending anything. That
# narrows the race with a board that comes back by itself from the length of
# a run (about 13 s) to the probe's own second.
#
# Every run ends with exactly one "RESULT <code> ..." line. The only
# state-changing command in this file is the single `rst -por`: there is no rst
# without -por (`rst -system` keeps debug state and takes the warm path that
# wedges this board), and no stop, con, mwr, rwr, dow or fpga.
#
# Attaching is safe only while cpu-sleep-0 is disabled (butters-kr260-deep-idle)
# or the board is already dead; see KR260_MANAGEMENT.md.

namespace eval kr260 {
    # The KR260 carrier's integrated FT4232H (channel A) and its JTAG chain.
    variable product SCK-KR
    variable devices {xck26 04724093 arm_dap 5ba00477}
    variable resets_issued 0
    variable last_result ""
    # Any TCP answer from this port, accepted or refused, means the board's
    # kernel is running. Its sshd accepts in about 1 ms on this LAN. Windows
    # reports a refusal only after retrying for about 2 s (measured
    # 2026-10-29), so a board whose kernel is up before sshd listens reads as
    # silent here; the agent's ICMP ping just before this run covers that.
    variable probe_port 22
    variable probe_ms 1000
    variable probe_state ""
}

proc kr260::result {code args} {
    variable last_result
    set last_result [string trim "$code [join $args { }]"]
    puts "RESULT $last_result"
    flush stdout
}

proc kr260::stamp {} {
    set ms [clock milliseconds]
    return "[clock format [expr {$ms / 1000}] -gmt 1 -format %Y-%m-%dT%H:%M:%S].[format %03d [expr {$ms % 1000}]]Z"
}

# A dotted IPv4 address without leading zeros (Tcl would read 010 as octal).
proc kr260::valid_address {address} {
    if {![regexp {^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$} $address]} {
        return 0
    }
    foreach octet [split $address .] {
        if {$octet > 255} { return 0 }
    }
    return 1
}

# dark only for silence (or the OS's own "host is unreachable"); reachable for
# an accepted or refused connection; unknown for anything else, which refuses
# exactly like a reachable board.
proc kr260::probe_verdict {state error} {
    if {$state eq "silent"} { return dark }
    if {$state ne "answered"} { return unknown }
    if {$error eq "" || [string match -nocase "*refused*" $error]} { return reachable }
    if {[string match -nocase "*host is unreachable*" $error]
            || [string match -nocase "*timed out*" $error]} {
        return dark
    }
    return unknown
}

proc kr260::board_probe {address} {
    variable probe_port
    variable probe_ms
    variable probe_state
    if {[catch {socket -async $address $probe_port} sock]} {
        return [probe_verdict failed $sock]
    }
    set probe_state waiting
    set error ""
    try {
        fileevent $sock writable [list set ::kr260::probe_state answered]
        set timer [after $probe_ms [list set ::kr260::probe_state silent]]
        vwait ::kr260::probe_state
        after cancel $timer
        if {$probe_state eq "answered"} {
            set error [fconfigure $sock -error]
            if {$error eq "" && ![catch {fconfigure $sock -connecting} connecting]
                    && $connecting} {
                set probe_state pending
            }
        }
    } finally {
        catch {close $sock}
    }
    return [probe_verdict $probe_state $error]
}

# 1 if the board is still dark at this moment; otherwise reports the refusal.
proc kr260::still_dark {address moment} {
    if {[catch {board_probe $address} verdict]} {
        set verdict unknown
    }
    puts "PROBE $moment $verdict at=[stamp]"
    flush stdout
    switch -- $verdict {
        dark { return 1 }
        reachable { result REFUSED_REACHABLE $moment }
        default { result REFUSED_PROBE $moment $verdict }
    }
    return 0
}

# `jtag targets` lists cables at indent < 4 and their devices deeper:
#   "  1  Xilinx SCK-KR XFL11R5N0BBZA"
#   "     2  xck26 (idcode 04724093 irlen 12 fpga)"
proc kr260::chain_problem {listing serial} {
    variable product
    variable devices
    set cables {}
    set found {}
    foreach line [split $listing \n] {
        if {![regexp {^( *)\d+\*?\s+(\S.*?)\s*$} $line -> indent text]} continue
        if {[string length $indent] < 4} {
            lappend cables $text
        } elseif {[regexp {^(\S+)\s+\(idcode\s+([0-9a-fA-F]+)} $text -> name idcode]} {
            lappend found $name [string tolower $idcode]
        }
    }
    if {[llength $cables] != 1} {
        return "expected exactly one JTAG cable, found [llength $cables]"
    }
    set cable [lindex $cables 0]
    if {[string first $product $cable] < 0 || [lindex $cable end] ne $serial} {
        return "cable '$cable' is not $product $serial"
    }
    if {$found ne $devices} {
        return "chain '$found' is not '$devices'"
    }
    return ""
}

proc kr260::main {argv} {
    variable resets_issued
    set mode [lindex $argv 0]
    set serial [lindex $argv 1]
    set address [lindex $argv 2]
    if {$mode ni {check por} || ![regexp {^[A-Z0-9]{12}A$} $serial]
            || ![valid_address $address] || [llength $argv] > 4} {
        result REFUSED_USAGE "mode=$mode serial=$serial address=$address"
        return
    }
    if {$mode eq "por" && [lindex $argv 3] ne "confirm-por"} {
        result REFUSED_UNCONFIRMED
        return
    }
    if {$mode eq "check" && [llength $argv] != 3} {
        result REFUSED_USAGE "check takes no confirmation"
        return
    }
    # Attaching to a running board with deep idle enabled hangs it.
    if {![still_dark $address before_attach]} {
        return
    }
    if {[catch {connect} err]} {
        result CONNECT_FAILED $err
        return
    }
    try {
        # hw_server scans asynchronously; an immediate query can be empty.
        set listing ""
        for {set i 0} {$i < 10} {incr i} {
            after 1000
            set listing [jtag targets]
            if {[string first $serial $listing] >= 0} break
        }
        puts $listing
        set problem [chain_problem $listing $serial]
        if {$problem ne ""} {
            result REFUSED_CHAIN $problem
            return
        }
        if {[catch {targets -set -nocase -filter {name == "PSU"}} err]} {
            result REFUSED_TARGET $err
            return
        }
        set selected [targets]
        puts $selected
        if {![regexp -line {^\s*\d+\*\s+PSU\s*$} $selected]} {
            result REFUSED_TARGET "PSU is not the active target"
            return
        }
        if {$mode eq "check"} {
            if {[still_dark $address attached]} {
                result CHECK_OK cable=$serial
            }
            return
        }
        if {[incr resets_issued] != 1} {
            result REFUSED_REPEAT
            return
        }
        # The last check before the reset: nothing but the time stamp between.
        if {![still_dark $address attached]} {
            return
        }
        set at [stamp]
        if {[catch {rst -por} err]} {
            result RST_ERROR at=$at $err
        } else {
            result RST_ISSUED at=$at cable=$serial
        }
    } finally {
        # Detach at once: the runtime deep-idle setting is gone after a reset,
        # so no debugger may stay attached while the board boots.
        catch {disconnect}
    }
}

if {![info exists ::kr260_test]} {
    kr260::main $::argv
}

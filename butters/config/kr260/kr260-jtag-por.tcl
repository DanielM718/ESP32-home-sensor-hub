# KR260 guarded JTAG power-on reset, run by XSDB on the Windows desktop.
#   xsdb -no-ini kr260-jtag-por.tcl check <cable-serial>
#   xsdb -no-ini kr260-jtag-por.tcl por   <cable-serial> confirm-por
# `check` attaches, validates the cable and chain, selects the target and
# detaches. `por` does the same, then pulses the cable's POR line once.
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
    if {$mode ni {check por} || ![regexp {^[A-Z0-9]{12}A$} $serial] || [llength $argv] > 3} {
        result REFUSED_USAGE "mode=$mode serial=$serial"
        return
    }
    if {$mode eq "por" && [lindex $argv 2] ne "confirm-por"} {
        result REFUSED_UNCONFIRMED
        return
    }
    if {$mode eq "check" && [llength $argv] != 2} {
        result REFUSED_USAGE "check takes no confirmation"
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
            result CHECK_OK cable=$serial
            return
        }
        if {[incr resets_issued] != 1} {
            result REFUSED_REPEAT
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

# Plain-Tcl harness for config/kr260/kr260-jtag-por.tcl. Run with a tclsh that
# has no XSDB commands (refuses to start otherwise), so nothing here can reach
# a JTAG cable:   tclsh kr260_jtag_por_harness.tcl <path-to-kr260-jtag-por.tcl>
foreach command {connect jtag targets rst disconnect stop con mwr dow fpga} {
    if {[llength [info commands $command]] > 0} {
        puts "HARNESS ABORT: '$command' exists; this is not a plain tclsh"
        exit 2
    }
}

set GOOD_CHAIN "  1  Xilinx SCK-KR XFL11R5N0BBZA\n     2  xck26 (idcode 04724093 irlen 12 fpga)\n     3  arm_dap (idcode 5ba00477 irlen 4)"
set PSU_SELECTED "  1  PS TAP\n     2  PMU\n     3  PL\n  4* PSU\n     5  RPU (Reset)\n     8  APU"
set PSU_UNSELECTED "  1  PS TAP\n  4  PSU\n     8  APU"

proc reset_fakes {args} {
    global calls fake
    set calls {}
    # probes: the verdicts of successive reachability probes, then "dark".
    array set fake {connect ok chain {} targets {} select ok rst ok probes {}}
    array set fake $args
}
proc connect {args} {
    global calls fake
    lappend calls connect
    if {$fake(connect) ne "ok"} { error "no hw_server" }
}
proc jtag {args} { global calls fake; lappend calls "jtag $args"; return $fake(chain) }
proc targets {args} {
    global calls fake
    lappend calls "targets $args"
    if {[llength $args] > 0 && $fake(select) ne "ok"} { error "no targets found with filter" }
    return $fake(targets)
}
proc rst {args} {
    global calls fake
    lappend calls "rst $args"
    if {$fake(rst) ne "ok"} { error "POR unsupported by cable" }
}
proc disconnect {args} { global calls; lappend calls disconnect }
rename after real_after
proc after {ms args} {}

set ::kr260_test 1
source [lindex $argv 0]

# The real probe opens a socket; these tests script its verdicts instead. The
# socket path itself is exercised against local listeners further down.
rename kr260::board_probe kr260::real_board_probe
proc kr260::board_probe {address} {
    global calls fake
    lappend calls "probe $address"
    set verdict [lindex $fake(probes) 0]
    set fake(probes) [lrange $fake(probes) 1 end]
    if {$verdict eq ""} { return dark }
    if {$verdict eq "error"} { error "probe blew up" }
    return $verdict
}

set failures 0
proc check {name cond detail} {
    global failures
    if {[uplevel 1 [list expr $cond]]} { puts "PASS $name" } else { puts "FAIL $name: $detail"; incr failures }
}
proc rsts {} { global calls; return [lsearch -all -inline -glob $calls "rst*"] }
proc run {args} { set ::kr260::resets_issued 0; kr260::main $args; return $::kr260::last_result }
set B 192.168.1.125

# --- success paths
reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
set r [run check XFL11R5N0BBZA $B]
check check_ok {[string match "CHECK_OK*" $r]} $r
check check_never_resets {[llength [rsts]] == 0} [rsts]
check check_disconnects {[lindex $calls end] eq "disconnect"} $calls
check check_probes_before_attach_and_while_attached {[lindex $calls 0] eq "probe $B"
    && [lindex $calls 1] eq "connect" && [lindex $calls end-1] eq "probe $B"} $calls

reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
set r [run por XFL11R5N0BBZA $B confirm-por]
check por_issued {[string match "RST_ISSUED at=*Z cable=XFL11R5N0BBZA" $r]} $r
check por_exactly_once {[rsts] eq {{rst -por}}} [rsts]
check por_disconnects_after_rst {[lindex $calls end] eq "disconnect" && [lindex $calls end-1] eq "rst -por"} $calls
check por_last_probe_immediately_precedes_rst {[lindex $calls end-2] eq "probe $B"} $calls
check por_first_probe_precedes_attach {[lindex $calls 0] eq "probe $B" && [lindex $calls 1] eq "connect"} $calls
check por_probes_exactly_twice {[llength [lsearch -all $calls "probe *"]] == 2} $calls

reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED rst fail
set r [run por XFL11R5N0BBZA $B confirm-por]
check rst_error_reported {[string match "RST_ERROR*POR unsupported*" $r]} $r
check rst_error_no_fallback {[rsts] eq {{rst -por}}} [rsts]
check rst_error_disconnects {[lindex $calls end] eq "disconnect"} $calls

# --- refusals: none may issue any rst
foreach {name argv_ chain targets_ extra expect} [list \
    no_confirmation [list por XFL11R5N0BBZA $B] $GOOD_CHAIN $PSU_SELECTED {} REFUSED_UNCONFIRMED \
    wrong_confirmation [list por XFL11R5N0BBZA $B yes] $GOOD_CHAIN $PSU_SELECTED {} REFUSED_UNCONFIRMED \
    bad_mode [list system XFL11R5N0BBZA $B] $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    bad_serial [list por XFL11R5N0BBZ $B confirm-por] $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    check_with_extra [list check XFL11R5N0BBZA $B confirm-por] $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    empty_chain [list por XFL11R5N0BBZA $B confirm-por] {} $PSU_SELECTED {} REFUSED_CHAIN \
    other_cable [list por XFL11R5N0BBZA $B confirm-por] [string map {XFL11R5N0BBZA XFL1EAUJ5SPOA} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    two_cables [list por XFL11R5N0BBZA $B confirm-por] "$GOOD_CHAIN\n  4  Digilent JTAG-SMT2 210251A08870\n     5  xc7z020 (idcode 23727093 irlen 6 fpga)" $PSU_SELECTED {} REFUSED_CHAIN \
    not_kria_product [list por XFL11R5N0BBZA $B confirm-por] [string map {SCK-KR SCK-XX} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    wrong_idcode [list por XFL11R5N0BBZA $B confirm-por] [string map {04724093 04721093} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    missing_dap [list por XFL11R5N0BBZA $B confirm-por] [join [lrange [split $GOOD_CHAIN \n] 0 1] \n] $PSU_SELECTED {} REFUSED_CHAIN \
    select_fails [list por XFL11R5N0BBZA $B confirm-por] $GOOD_CHAIN $PSU_SELECTED {select fail} REFUSED_TARGET \
    psu_not_active [list por XFL11R5N0BBZA $B confirm-por] $GOOD_CHAIN $PSU_UNSELECTED {} REFUSED_TARGET \
    no_hw_server [list por XFL11R5N0BBZA $B confirm-por] $GOOD_CHAIN $PSU_SELECTED {connect fail} CONNECT_FAILED \
] {
    reset_fakes chain $chain targets $targets_ {*}$extra
    set r [run {*}$argv_]
    check $name {[string match "$expect*" $r]} $r
    check ${name}_no_rst {[llength [rsts]] == 0} [rsts]
    if {$expect ni {REFUSED_USAGE REFUSED_UNCONFIRMED CONNECT_FAILED}} {
        check ${name}_disconnects {[lindex $calls end] eq "disconnect"} $calls
    } else {
        check ${name}_no_attach_for_bad_args {$expect eq "CONNECT_FAILED" || [lsearch $calls connect] < 0} $calls
    }
}

# --- reachability: a board that answers is never attached to or reset
foreach {name mode probes expect attached} {
    reachable_before_attach_por por {reachable} {REFUSED_REACHABLE before_attach} 0
    reachable_before_attach_check check {reachable} {REFUSED_REACHABLE before_attach} 0
    unknown_before_attach por {unknown} {REFUSED_PROBE before_attach unknown} 0
    probe_error_before_attach por {error} {REFUSED_PROBE before_attach unknown} 0
    came_back_while_attached_por por {dark reachable} {REFUSED_REACHABLE attached} 1
    came_back_while_attached_check check {dark reachable} {REFUSED_REACHABLE attached} 1
    unknown_while_attached por {dark unknown} {REFUSED_PROBE attached unknown} 1
    probe_error_while_attached por {dark error} {REFUSED_PROBE attached unknown} 1
} {
    reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED probes $probes
    if {$mode eq "por"} {
        set r [run por XFL11R5N0BBZA $B confirm-por]
    } else {
        set r [run check XFL11R5N0BBZA $B]
    }
    check $name {$r eq $expect} $r
    check ${name}_no_rst {[llength [rsts]] == 0} [rsts]
    if {$attached} {
        check ${name}_detaches {[lindex $calls end] eq "disconnect"} $calls
    } else {
        check ${name}_never_attaches {[lsearch $calls connect] < 0} $calls
    }
}

foreach {name address} {
    no_address {}
    hostname kria.local
    leading_zero 192.168.001.125
    octet_too_big 192.168.1.256
    five_octets 192.168.1.125.1
    trailing_space {192.168.1.125 }
} {
    reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
    set r [run por XFL11R5N0BBZA $address confirm-por]
    check bad_address_$name {[string match "REFUSED_USAGE*" $r] && [llength $calls] == 0} "$r $calls"
}

foreach {state error expect} {
    silent {} dark
    answered {} reachable
    answered {connection refused} reachable
    answered {host is unreachable} dark
    answered {connection timed out} dark
    answered {network is unreachable} unknown
    answered {permission denied} unknown
    pending {} unknown
    failed {couldn't open socket: invalid argument} unknown
    waiting {} unknown
} {
    set v [kr260::probe_verdict $state $error]
    check "verdict_${state}_[string map {{ } _ : {} ' {}} $error]" {$v eq $expect} "$state/$error -> $v"
}

# --- the real socket probe against local listeners (no XSDB, no board)
rename after fake_after
rename real_after after
set listener [socket -server {apply {{chan addr port} {close $chan}}} -myaddr 127.0.0.1 0]
set port [lindex [fconfigure $listener -sockname] 2]
set ::kr260::probe_port $port
check real_probe_accepted_is_reachable {[kr260::real_board_probe 127.0.0.1] eq "reachable"} $port
close $listener
# Windows retries a refused connect for about 2 s before reporting it.
set ::kr260::probe_ms 3000
check real_probe_refused_is_reachable {[kr260::real_board_probe 127.0.0.1] eq "reachable"} $port
set ::kr260::probe_ms 1000
if {[info exists ::env(KR260_SILENT_ADDRESS)]} {
    set t0 [clock milliseconds]
    set v [kr260::real_board_probe $::env(KR260_SILENT_ADDRESS)]
    set elapsed [expr {[clock milliseconds] - $t0}]
    check real_probe_silence_is_dark {$v eq "dark" && $elapsed < 1500} "$v ${elapsed}ms"
}
set ::kr260::probe_port 22
rename after real_after
rename fake_after after

# --- a second reset within one process is refused
reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
kr260::main [list por XFL11R5N0BBZA $B confirm-por]
kr260::main [list por XFL11R5N0BBZA $B confirm-por]
check second_por_refused {[string match "REFUSED_REPEAT*" $::kr260::last_result] && [llength [rsts]] == 1} "$::kr260::last_result [rsts]"

puts "SUMMARY failures=$failures"
exit [expr {$failures ? 1 : 0}]

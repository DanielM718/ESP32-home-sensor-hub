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
    array set fake {connect ok chain {} targets {} select ok rst ok}
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

set failures 0
proc check {name cond detail} {
    global failures
    if {[uplevel 1 [list expr $cond]]} { puts "PASS $name" } else { puts "FAIL $name: $detail"; incr failures }
}
proc rsts {} { global calls; return [lsearch -all -inline -glob $calls "rst*"] }
proc run {args} { set ::kr260::resets_issued 0; kr260::main $args; return $::kr260::last_result }

# --- success paths
reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
set r [run check XFL11R5N0BBZA]
check check_ok {[string match "CHECK_OK*" $r]} $r
check check_never_resets {[llength [rsts]] == 0} [rsts]
check check_disconnects {[lindex $calls end] eq "disconnect"} $calls

reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
set r [run por XFL11R5N0BBZA confirm-por]
check por_issued {[string match "RST_ISSUED at=*Z cable=XFL11R5N0BBZA" $r]} $r
check por_exactly_once {[rsts] eq {{rst -por}}} [rsts]
check por_disconnects_after_rst {[lindex $calls end] eq "disconnect" && [lindex $calls end-1] eq "rst -por"} $calls

reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED rst fail
set r [run por XFL11R5N0BBZA confirm-por]
check rst_error_reported {[string match "RST_ERROR*POR unsupported*" $r]} $r
check rst_error_no_fallback {[rsts] eq {{rst -por}}} [rsts]
check rst_error_disconnects {[lindex $calls end] eq "disconnect"} $calls

# --- refusals: none may issue any rst
foreach {name argv_ chain targets_ extra expect} [list \
    no_confirmation {por XFL11R5N0BBZA} $GOOD_CHAIN $PSU_SELECTED {} REFUSED_UNCONFIRMED \
    wrong_confirmation {por XFL11R5N0BBZA yes} $GOOD_CHAIN $PSU_SELECTED {} REFUSED_UNCONFIRMED \
    bad_mode {system XFL11R5N0BBZA} $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    bad_serial {por XFL11R5N0BBZ confirm-por} $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    check_with_extra {check XFL11R5N0BBZA confirm-por} $GOOD_CHAIN $PSU_SELECTED {} REFUSED_USAGE \
    empty_chain {por XFL11R5N0BBZA confirm-por} {} $PSU_SELECTED {} REFUSED_CHAIN \
    other_cable {por XFL11R5N0BBZA confirm-por} [string map {XFL11R5N0BBZA XFL1EAUJ5SPOA} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    two_cables {por XFL11R5N0BBZA confirm-por} "$GOOD_CHAIN\n  4  Digilent JTAG-SMT2 210251A08870\n     5  xc7z020 (idcode 23727093 irlen 6 fpga)" $PSU_SELECTED {} REFUSED_CHAIN \
    not_kria_product {por XFL11R5N0BBZA confirm-por} [string map {SCK-KR SCK-XX} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    wrong_idcode {por XFL11R5N0BBZA confirm-por} [string map {04724093 04721093} $GOOD_CHAIN] $PSU_SELECTED {} REFUSED_CHAIN \
    missing_dap {por XFL11R5N0BBZA confirm-por} [join [lrange [split $GOOD_CHAIN \n] 0 1] \n] $PSU_SELECTED {} REFUSED_CHAIN \
    select_fails {por XFL11R5N0BBZA confirm-por} $GOOD_CHAIN $PSU_SELECTED {select fail} REFUSED_TARGET \
    psu_not_active {por XFL11R5N0BBZA confirm-por} $GOOD_CHAIN $PSU_UNSELECTED {} REFUSED_TARGET \
    no_hw_server {por XFL11R5N0BBZA confirm-por} $GOOD_CHAIN $PSU_SELECTED {connect fail} CONNECT_FAILED \
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

# --- a second reset within one process is refused
reset_fakes chain $GOOD_CHAIN targets $PSU_SELECTED
kr260::main {por XFL11R5N0BBZA confirm-por}
kr260::main {por XFL11R5N0BBZA confirm-por}
check second_por_refused {[string match "REFUSED_REPEAT*" $::kr260::last_result] && [llength [rsts]] == 1} "$::kr260::last_result [rsts]"

puts "SUMMARY failures=$failures"
exit [expr {$failures ? 1 : 0}]

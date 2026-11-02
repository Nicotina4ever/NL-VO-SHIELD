// Impedisce la console finestra nelle build release Windows.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    nl_vo_shield_lib::run()
}

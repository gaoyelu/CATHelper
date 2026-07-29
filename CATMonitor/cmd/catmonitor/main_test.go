package main

import (
	"testing"

	"github.com/Computing-Availability-Tools/CATMonitor/features/health/stress"
)

func TestRunStressCommandExitCodes(t *testing.T) {
	if code := runStress(nil); code != 0 {
		t.Fatalf("empty stress command code=%d want 0", code)
	}
	if code := runStress([]string{"--help"}); code != 0 {
		t.Fatalf("help code=%d want 0", code)
	}
	if code := runStress([]string{"bad-subcommand"}); code != 2 {
		t.Fatalf("bad subcommand code=%d want 2", code)
	}
	if code := runStress([]string{"run", "--bad-option"}); code != 2 {
		t.Fatalf("bad option code=%d want 2", code)
	}
}

func TestStressTableFormatting(t *testing.T) {
	tests := []struct {
		status stress.Status
		want   string
	}{
		{stress.StatusHealthy, "OK"},
		{stress.StatusTimeLimitReached, "OK (time limit)"},
		{stress.StatusUnhealthy, "FAILED"},
		{stress.StatusCancelled, "CANCELLED"},
	}
	for _, test := range tests {
		if got := stressStatusLabel(test.status); got != test.want {
			t.Errorf("stressStatusLabel(%q)=%q want %q", test.status, got, test.want)
		}
	}
	if got := formatStressDuration(1117); got != "1.117s" {
		t.Errorf("formatStressDuration(1117)=%q", got)
	}
	if got := formatStressValue(50000); got != "50000" {
		t.Errorf("formatStressValue integer=%q", got)
	}
	if got := formatStressValue(123.456); got != "123.46" {
		t.Errorf("formatStressValue decimal=%q", got)
	}
}

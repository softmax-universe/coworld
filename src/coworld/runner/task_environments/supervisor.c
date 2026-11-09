#define _DEFAULT_SOURCE
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <grp.h>
#include <stdint.h>
#include <string.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/prctl.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* PID 1 owns the namespace lifetime, even when the container runtime cannot
 * service a stop request. Exec'd task processes cannot replace this deadline. */
static void reap(int signal_number) {
  (void)signal_number;
  int saved_errno = errno;
  while (waitpid(-1, NULL, WNOHANG) > 0) {}
  errno = saved_errno;
}

static void stop(int signal_number) {
  (void)signal_number;
  _exit(0);
}

static unsigned long identity_number(const char *value) {
  char *end;
  errno = 0;
  unsigned long number = strtoul(value, &end, 10);
  if (errno || !*value || *end || *value == '-' || number >= UINT32_MAX) {
    fputs("invalid task identity\n", stderr);
    exit(1);
  }
  return number;
}

int main(int argc, char **argv) {
  /* Only coordinator exec starts this path as root. Task commands receive their
   * declared identity before executing any image-owned program. */
  if (argc >= 7 && strcmp(argv[1], "--user") == 0) {
    uid_t uid = (uid_t)identity_number(argv[2]);
    gid_t gid = (gid_t)identity_number(argv[3]);
    unsigned long count = identity_number(argv[5]);
    if (argv[4][0] != '/' || count > 64 || argc <= 6 + (int)count) {
      fputs("invalid task identity groups\n", stderr);
      return 1;
    }
    gid_t groups[64];
    for (unsigned long i = 0; i < count; i++)
      groups[i] = (gid_t)identity_number(argv[6 + i]);
    if (setenv("HOME", argv[4], 1) || setgroups(count, groups) || setgid(gid) || setuid(uid) ||
        prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)) {
      perror("set task identity");
      return 1;
    }
    execvp(argv[6 + count], argv + 6 + count);
    perror("exec task command");
    return 1;
  }
  if (argc != 2 || getpid() != 1) {
    fputs("task supervisor requires PID 1 and an absolute expiry\n", stderr);
    return 1;
  }
  char *end;
  errno = 0;
  long expiry = strtol(argv[1], &end, 10);
  if (errno || *end || expiry <= 0 || prctl(PR_SET_DUMPABLE, 0) != 0) {
    fputs("invalid task lifetime\n", stderr);
    return 1;
  }
  struct sigaction child = {.sa_handler = reap, .sa_flags = SA_NOCLDSTOP};
  struct sigaction terminate = {.sa_handler = stop};
  if (sigemptyset(&child.sa_mask) || sigemptyset(&terminate.sa_mask) ||
      sigaction(SIGCHLD, &child, NULL) || sigaction(SIGTERM, &terminate, NULL) ||
      sigaction(SIGINT, &terminate, NULL)) {
    perror("task signal handlers");
    return 1;
  }
  struct timespec deadline = {.tv_sec = expiry};
  int result;
  do {
    result = clock_nanosleep(CLOCK_REALTIME, TIMER_ABSTIME, &deadline, NULL);
  } while (result == EINTR);
  if (result) {
    errno = result;
    perror("task deadline");
    return 1;
  }
  _exit(124);
}
